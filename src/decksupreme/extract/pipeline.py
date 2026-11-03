"""Task 1 entry point: leaderboard -> battle logs -> one raw JSON object in MinIO.

`run_extraction()` returns a small JSON-serialisable summary, so an Airflow
@task can return it directly as XCom. The task fails only when nothing usable
was collected (empty leaderboard, every battle log failed, or an auth error).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from decksupreme.extract.client import ClashRoyaleClient
from decksupreme.extract.storage import RawStorage, iso_year_week

log = logging.getLogger(__name__)

PAYLOAD_SCHEMA_VERSION = 1


class ExtractionError(RuntimeError):
    pass


def build_payload(
    *,
    extracted_at: datetime,
    source: str,
    leaderboard_source: str,
    leaderboard_season: str | None,
    leaderboard: list[dict[str, Any]],
    logs: dict[str, list[dict[str, Any]]],
    failures: dict[str, str],
) -> dict[str, Any]:
    year, week = iso_year_week(extracted_at)
    order = {p["tag"]: i for i, p in enumerate(leaderboard)}
    battle_count = sum(len(b) for b in logs.values())
    return {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "extracted_at": extracted_at.isoformat(timespec="seconds"),
        "year": year,
        "week": week,
        "source": source,
        "leaderboard_source": leaderboard_source,
        "leaderboard_season": leaderboard_season,
        "stats": {
            "players_requested": len(leaderboard),
            "players_succeeded": len(logs),
            "players_failed": len(failures),
            "battle_count": battle_count,
        },
        "leaderboard": leaderboard,
        "battle_logs": [
            {"player_tag": tag, "battles": logs[tag]}
            for tag in sorted(logs, key=lambda t: order.get(t, len(order)))
        ],
        "failures": [{"player_tag": t, "error": e} for t, e in sorted(failures.items())],
    }


def run_extraction(
    *,
    limit: int = 1000,
    max_workers: int = 8,
    upload: bool = True,
    client: ClashRoyaleClient | None = None,
    storage: RawStorage | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    extracted_at = (now or datetime.now(UTC)).astimezone(UTC)
    client = client or ClashRoyaleClient()

    board = client.get_leaderboard(limit, now=extracted_at)
    players = board.players
    if not players:
        raise ExtractionError("leaderboard is empty; nothing to extract")
    log.info("leaderboard: %d players from %s", len(players), board.source)

    batch = client.fetch_battle_logs([p.tag for p in players], max_workers=max_workers)
    log.info(
        "battle logs: %d ok, %d failed, %d battles",
        len(batch.logs), len(batch.failures), batch.battle_count,
    )
    if not batch.logs:
        raise ExtractionError(f"all {len(players)} battle-log requests failed")

    payload = build_payload(
        extracted_at=extracted_at,
        source=client.base_url,
        leaderboard_source=board.source,
        leaderboard_season=board.season,
        leaderboard=[p.to_dict() for p in players],
        logs=batch.logs,
        failures=batch.failures,
    )
    summary: dict[str, Any] = dict(
        payload["stats"],
        year=payload["year"],
        week=payload["week"],
        leaderboard_season=board.season,
    )

    if upload:
        stored = (storage or RawStorage()).put_raw_battles(
            payload, record_count=batch.battle_count, extracted_at=extracted_at
        )
        log.info("wrote %s (%d bytes)", stored.uri, stored.size_bytes)
        summary.update(uri=stored.uri, key=stored.key, size_bytes=stored.size_bytes)
    else:
        summary["payload"] = payload
    return summary
