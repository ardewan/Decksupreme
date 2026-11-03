"""ClashRoyaleClient tests against a fake requests.Session (no network)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

from decksupreme.config import ClashSettings
from decksupreme.extract.client import (
    BattleLogBatch,
    ClashApiError,
    ClashAuthError,
    ClashRoyaleClient,
    TokenBucket,
    encode_tag,
    guess_completed_seasons,
)

SETTINGS = ClashSettings(base_url="https://api.test/v1", max_requests_per_second=30, api_key="k")


class FakeResponse:
    def __init__(self, status: int, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    """Routes URL -> list of responses (or exceptions), consumed in order."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        queue = self.routes[url]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


class NoLimit:
    def acquire(self):
        pass


def make_client(routes, **kw):
    sleeps = []
    session = FakeSession(routes)
    client = ClashRoyaleClient(
        SETTINGS, session=session, rate_limiter=NoLimit(), sleep=sleeps.append, **kw
    )
    return client, session, sleeps


def url(path):
    return f"https://api.test/v1{path}"


def test_encode_tag():
    assert encode_tag("#2pp") == "%232PP"
    assert encode_tag("2PP") == "%232PP"


def test_auth_header_set():
    _, session, _ = make_client({})
    assert session.headers["Authorization"] == "Bearer k"


def test_retries_transient_then_succeeds():
    client, session, sleeps = make_client(
        {
            url("/players/%23A/battlelog"): [
                FakeResponse(503),
                FakeResponse(429, headers={"Retry-After": "2"}),
                requests.ConnectionError("reset"),
                FakeResponse(200, [{"type": "PvP"}]),
            ]
        }
    )
    assert client.get_battle_log("#A") == [{"type": "PvP"}]
    assert len(session.calls) == 4
    assert len(sleeps) == 3
    assert sleeps[1] >= 2  # Retry-After honoured


def test_backoff_is_exponential_with_jitter_and_capped():
    client, _, _ = make_client({}, backoff_base=1, backoff_cap=5)
    for attempt in range(10):
        d = client._backoff(attempt, None)
        assert 0 <= d <= min(5, 2**attempt)


def test_gives_up_after_max_retries():
    client, session, sleeps = make_client(
        {url("/players/%23A/battlelog"): [FakeResponse(500)]}, max_retries=3
    )
    with pytest.raises(ClashApiError, match="gave up after 4 attempts"):
        client.get_battle_log("#A")
    assert len(session.calls) == 4
    assert len(sleeps) == 3


def test_404_not_retried():
    client, session, _ = make_client({url("/players/%23A/battlelog"): [FakeResponse(404)]})
    with pytest.raises(ClashApiError) as exc:
        client.get_battle_log("#A")
    assert exc.value.status == 404
    assert len(session.calls) == 1


def test_403_raises_auth_error():
    client, _, _ = make_client({url("/players/%23A/battlelog"): [FakeResponse(403)]})
    with pytest.raises(ClashAuthError):
        client.get_battle_log("#A")


def item(i):
    return {"tag": f"#P{i}", "name": f"n{i}", "rank": i}


LIVE = "/locations/global/pathoflegend/players"
SEASONS = "/locations/global/seasonsV2"
NOW = datetime(2026, 11, 2, 15, tzinfo=UTC)
SEASON_LIST = {
    "items": [
        {"code": "2026-09", "endTime": "20261005T090000.000Z"},
        {"code": "2026-10", "endTime": "20261102T090000.000Z"},
        {"code": "2026-11", "endTime": "20261207T090000.000Z"},  # not ended yet
    ]
}


def test_leaderboard_follows_cursor():
    client, session, _ = make_client(
        {
            url(LIVE): [
                FakeResponse(
                    200, {"items": [item(1), item(2)], "paging": {"cursors": {"after": "c"}}}
                ),
                FakeResponse(200, {"items": [item(3)], "paging": {"cursors": {}}}),
            ]
        }
    )
    board = client.get_leaderboard(limit=3, now=NOW)
    assert [(p.tag, p.rank) for p in board.players] == [("#P1", 1), ("#P2", 2), ("#P3", 3)]
    assert (board.source, board.season) == (LIVE, None)
    assert session.calls[0][1] == {"limit": 3}
    assert session.calls[1][1] == {"limit": 1, "after": "c"}


def test_latest_completed_season_skips_future_seasons():
    client, _, _ = make_client({url(SEASONS): [FakeResponse(200, SEASON_LIST)]})
    assert client.latest_completed_season(NOW) == "2026-10"


def test_leaderboard_falls_back_to_last_season_when_live_is_short():
    season_path = "/locations/global/pathoflegend/2026-10/rankings/players"
    client, _, _ = make_client(
        {
            url(LIVE): [FakeResponse(200, {"items": [item(1)], "paging": {"cursors": {}}})],
            url(SEASONS): [FakeResponse(200, SEASON_LIST)],
            url(season_path): [
                FakeResponse(200, {"items": [item(i) for i in range(1, 4)], "paging": {}})
            ],
        }
    )
    board = client.get_leaderboard(limit=3, now=NOW)
    assert (board.source, board.season) == (season_path, "2026-10")
    assert len(board.players) == 3


def test_fan_out_records_failures_and_continues():
    client, _, _ = make_client(
        {
            url("/players/%23A/battlelog"): [FakeResponse(200, [{"b": 1}, {"b": 2}])],
            url("/players/%23B/battlelog"): [FakeResponse(404)],
            url("/players/%23C/battlelog"): [FakeResponse(200, [{"b": 3}])],
        },
        max_retries=0,
    )
    batch = client.fetch_battle_logs(["#A", "#B", "#C", "#A"], max_workers=3)
    assert isinstance(batch, BattleLogBatch)
    assert set(batch.logs) == {"#A", "#C"}
    assert set(batch.failures) == {"#B"}
    assert batch.battle_count == 3


def test_fan_out_aborts_on_auth_error():
    client, _, _ = make_client({url("/players/%23A/battlelog"): [FakeResponse(403)]})
    with pytest.raises(ClashAuthError):
        client.fetch_battle_logs(["#A"])


def test_rps_capped_at_30():
    s = ClashSettings(base_url="https://x", max_requests_per_second=100, api_key="k")
    client = ClashRoyaleClient(s, session=FakeSession({}))
    assert client.rate_limiter.rate == 30


def test_token_bucket_paces_requests():
    t = [0.0]

    def sleep(d):
        t[0] += d

    bucket = TokenBucket(rate=10, capacity=1, clock=lambda: t[0], sleep=sleep)
    for _ in range(21):
        bucket.acquire()
    # first token is free, the next 20 cost 0.1s each
    assert t[0] == pytest.approx(2.0)


# -- season fallback (the API's season list has been seen returning all-null entries) --
NULL_SEASONS = {"items": [{"code": None, "uniqueId": None, "endTime": None}] * 3}


def test_latest_completed_season_ignores_null_entries():
    client, _, _ = make_client({url(SEASONS): [FakeResponse(200, NULL_SEASONS)]})
    assert client.latest_completed_season(NOW) is None


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 3, 7, tzinfo=UTC), ["2026-10", "2026-09"]),   # after Mon Nov 2 09:00
        (datetime(2026, 11, 2, 8, tzinfo=UTC), ["2026-09", "2026-08"]),   # an hour before reset
        (datetime(2026, 11, 2, 9, tzinfo=UTC), ["2026-10", "2026-09"]),   # exactly at reset
        (datetime(2026, 10, 5, 10, tzinfo=UTC), ["2026-09", "2026-08"]),  # Oct: 1st Mon is 5th
        (datetime(2027, 1, 2, tzinfo=UTC), ["2026-11", "2026-10"]),       # across new year
        (datetime(2027, 1, 4, 12, tzinfo=UTC), ["2026-12", "2026-11"]),
    ],
)
def test_guess_completed_seasons(now, expected):
    assert guess_completed_seasons(now) == expected


def test_leaderboard_uses_calendar_guess_when_season_list_is_null():
    s09 = "/locations/global/pathoflegend/2026-09/rankings/players"
    s10 = "/locations/global/pathoflegend/2026-10/rankings/players"
    now = datetime(2026, 11, 3, 7, tzinfo=UTC)
    client, session, _ = make_client(
        {
            url(LIVE): [FakeResponse(200, {"items": [item(1)], "paging": {}})],
            url(SEASONS): [FakeResponse(200, NULL_SEASONS)],
            url(s10): [FakeResponse(404)],
            url(s09): [FakeResponse(200, {"items": [item(i) for i in range(1, 4)], "paging": {}})],
        }
    )
    board = client.get_leaderboard(limit=3, now=now)
    assert (board.season, len(board.players)) == ("2026-09", 3)
    assert [u for u, _ in session.calls].count(url(s10)) == 1  # 404 is not retried
