"""Clash Royale API client: leaderboard fetch + rate-limited battle-log fan-out.

* One shared token bucket paces every request across all worker threads, so the
  configured rate (CLASH_ROYALE_MAX_RPS, capped at 30) holds no matter how many
  threads run.
* Transient failures (429, 500, 502, 503, 504, timeouts, connection resets) are
  retried with exponential backoff and full jitter, honouring Retry-After.
* 401/403 mean the key is bad or the IP isn't whitelisted. Every later request
  would fail the same way, so they abort the run (ClashAuthError) rather than
  being logged 1,000 times.
* Any other per-player failure (404, retries exhausted) is logged and collected
  in BattleLogBatch.failures; the rest of the batch carries on.
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import requests

from decksupreme.config import ClashSettings, get_settings

log = logging.getLogger(__name__)

MAX_ALLOWED_RPS = 30.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# The trophy leaderboard (/locations/global/rankings/players) has returned an empty
# list since Supercell moved top-level ranking to Path of Legend. The live ladder
# resets each month, so early in a season it has only a handful of players; then
# we fall back to the final standings of the most recently ended season.
LIVE_LEADERBOARD_PATH = "/locations/global/pathoflegend/players"
SEASON_LEADERBOARD_PATH = "/locations/global/pathoflegend/{season}/rankings/players"
SEASONS_PATH = "/locations/global/seasonsV2"


class ClashApiError(RuntimeError):
    """A request failed permanently (non-retryable status or retries exhausted)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ClashAuthError(ClashApiError):
    """401/403: the key is invalid or not whitelisted for this public IP. Fatal."""


class TokenBucket:
    """Thread-safe token bucket. `acquire()` blocks until a token is available."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                # tolerance: float refill can land on 0.999… and spin forever
                if self._tokens >= 1 - 1e-9:
                    self._tokens = max(0.0, self._tokens - 1)
                    return
                wait = (1 - self._tokens) / self.rate
            self._sleep(wait)


@dataclass(frozen=True)
class RankedPlayer:
    tag: str
    name: str
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {"tag": self.tag, "name": self.name, "rank": self.rank}


@dataclass(frozen=True)
class Leaderboard:
    source: str  # API path the players came from
    season: str | None  # e.g. "2026-10" when the season fallback was used, else None (live)
    players: list[RankedPlayer]


@dataclass
class BattleLogBatch:
    logs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def battle_count(self) -> int:
        return sum(len(b) for b in self.logs.values())


def _month_code(year: int, month: int) -> str:
    while month < 1:
        year, month = year - 1, month + 12
    return f"{year:04d}-{month:02d}"


def guess_completed_seasons(now: datetime) -> list[str]:
    """Calendar fallback for when /seasonsV2 is unusable.

    Observed: Path of Legend season 'YYYY-MM' ends on the first Monday of the
    following month at 09:00 UTC (2026-09 -> Oct 5, 2026-10 -> Nov 2). Returns
    the most recently ended season and the one before it, newest first.
    """
    first = datetime(now.year, now.month, 1, 9, tzinfo=UTC)
    first_monday = first + timedelta(days=(7 - first.weekday()) % 7)
    back = 1 if now >= first_monday else 2
    return [_month_code(now.year, now.month - back), _month_code(now.year, now.month - back - 1)]


def encode_tag(tag: str) -> str:
    """'#2PP' or '2pp' -> '%232PP' (the API wants the '#' URL-encoded)."""
    tag = tag.strip().upper()
    if not tag.startswith("#"):
        tag = "#" + tag
    return quote(tag, safe="")


class ClashRoyaleClient:
    def __init__(
        self,
        settings: ClashSettings | None = None,
        *,
        session: requests.Session | None = None,
        rate_limiter: TokenBucket | None = None,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        timeout: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings or get_settings().clash
        self.base_url = self.settings.base_url.rstrip("/")
        rps = self.settings.max_requests_per_second
        if rps > MAX_ALLOWED_RPS:
            log.warning("CLASH_ROYALE_MAX_RPS=%s is above %s; capping it", rps, MAX_ALLOWED_RPS)
            rps = MAX_ALLOWED_RPS
        self.rate_limiter = rate_limiter or TokenBucket(rps)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.settings.require_api_key()}",
                "Accept": "application/json",
            }
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.timeout = timeout
        self._sleep = sleep

    # -- low level ---------------------------------------------------------
    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        delay = random.uniform(0, min(self.backoff_cap, self.backoff_base * 2**attempt))
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, float(retry_after))
        return delay

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last_error = ""
        for attempt in range(self.max_retries + 1):
            self.rate_limiter.acquire()
            retry_after = None
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 403):
                    raise ClashAuthError(
                        f"HTTP {resp.status_code} from {path}: API key is invalid or not "
                        "whitelisted for this machine's public IP",
                        resp.status_code,
                    )
                if resp.status_code not in RETRYABLE_STATUS:
                    raise ClashApiError(
                        f"HTTP {resp.status_code} from {path}: {resp.text[:200]}", resp.status_code
                    )
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
            if attempt < self.max_retries:
                delay = self._backoff(attempt, retry_after)
                log.debug("%s on %s; retry %d in %.2fs", last_error, path, attempt + 1, delay)
                self._sleep(delay)
        raise ClashApiError(f"{path}: gave up after {self.max_retries + 1} attempts ({last_error})")

    # -- endpoints ---------------------------------------------------------
    def _ranked_players(self, path: str, limit: int) -> list[RankedPlayer]:
        """Page through a ranking endpoint via `paging.cursors.after` until `limit`."""
        players: list[RankedPlayer] = []
        after: str | None = None
        while len(players) < limit:
            params: dict[str, Any] = {"limit": limit - len(players)}
            if after:
                params["after"] = after
            data = self.get(path, params)
            items = data.get("items", [])
            players.extend(
                RankedPlayer(tag=i["tag"], name=i.get("name", ""), rank=int(i["rank"]))
                for i in items
            )
            after = (data.get("paging") or {}).get("cursors", {}).get("after")
            if not items or not after:
                break
        return players[:limit]

    def latest_completed_season(self, now: datetime | None = None) -> str | None:
        """Code (e.g. '2026-10') of the most recent season whose endTime has passed,
        per GET /seasonsV2. None if the API returns nothing usable: it has been seen
        returning every entry as null."""
        now = now or datetime.now(UTC)
        ended = []
        for s in self.get(SEASONS_PATH).get("items", []):
            try:
                end = datetime.strptime(s["endTime"], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
            except (KeyError, TypeError, ValueError):
                continue
            if end <= now and isinstance(s.get("code"), str):
                ended.append((end, s["code"]))
        return max(ended)[1] if ended else None

    def _season_candidates(self, now: datetime) -> list[str]:
        """Seasons to try, newest first: the API's answer, else calendar guesses."""
        try:
            from_api = self.latest_completed_season(now)
        except ClashApiError as exc:
            if isinstance(exc, ClashAuthError):
                raise
            log.warning("season list unavailable (%s); guessing from the calendar", exc)
            from_api = None
        if from_api:
            return [from_api]
        log.warning("season list had no usable entries; guessing from the calendar")
        return guess_completed_seasons(now)

    def get_leaderboard(self, limit: int = 1000, now: datetime | None = None) -> Leaderboard:
        """Live Path of Legend top `limit`; if the season just reset and the live
        ladder is short, use the final standings of the last completed season."""
        now = now or datetime.now(UTC)
        live = self._ranked_players(LIVE_LEADERBOARD_PATH, limit)
        if len(live) >= limit:
            return Leaderboard(LIVE_LEADERBOARD_PATH, None, live)

        for season in self._season_candidates(now):
            path = SEASON_LEADERBOARD_PATH.format(season=season)
            try:
                final = self._ranked_players(path, limit)
            except ClashApiError as exc:
                if exc.status != 404:
                    raise
                log.info("season %s has no leaderboard (404); trying an earlier one", season)
                continue
            if len(final) > len(live):
                log.info(
                    "live ladder has %d/%d players; using season %s final standings (%d)",
                    len(live), limit, season, len(final),
                )
                return Leaderboard(path, season, final)

        log.warning("leaderboard has only %d/%d players", len(live), limit)
        return Leaderboard(LIVE_LEADERBOARD_PATH, None, live)

    def get_top_players(self, limit: int = 1000) -> list[RankedPlayer]:
        return self.get_leaderboard(limit).players

    def get_battle_log(self, tag: str) -> list[dict[str, Any]]:
        return self.get(f"/players/{encode_tag(tag)}/battlelog")

    def fetch_battle_logs(self, tags: Iterable[str], max_workers: int = 8) -> BattleLogBatch:
        """Fetch many battle logs at once. The token bucket caps the rate, not the
        thread count. ClashAuthError stops the batch; other errors are recorded."""
        tags = list(dict.fromkeys(tags))  # dedupe, keep order
        batch = BattleLogBatch()
        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self.get_battle_log, t): t for t in tags}
            try:
                for fut in as_completed(futures):
                    tag = futures[fut]
                    try:
                        batch.logs[tag] = fut.result()
                    except ClashAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - one player must not sink the batch
                        batch.failures[tag] = str(exc)
                        log.error("battle log failed for %s: %s", tag, exc)
                    done += 1
                    if done % 100 == 0:
                        log.info("battle logs: %d/%d done", done, len(tags))
            except ClashAuthError:
                for f in futures:
                    f.cancel()
                raise
        return batch
