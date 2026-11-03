"""RawStorage + run_extraction tests against moto's in-memory S3."""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from decksupreme.config import MinioSettings
from decksupreme.extract.client import BattleLogBatch, Leaderboard, RankedPlayer
from decksupreme.extract.pipeline import ExtractionError, run_extraction
from decksupreme.extract.storage import RawStorage, raw_battles_key

BUCKET = "decksupreme-raw"
MINIO = MinioSettings(
    endpoint="http://unused", bucket=BUCKET, region="us-east-1", access_key="a", secret_key="b"
)
WHEN = datetime(2026, 11, 2, 15, 30, tzinfo=UTC)  # ISO 2026-W45


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def test_key_uses_iso_year_and_week():
    assert raw_battles_key(WHEN) == "year=2026/week=45/raw_battles.json"
    # Jan 1 2027 is a Friday, so it falls in ISO 2026-W53
    assert raw_battles_key(datetime(2027, 1, 1)) == "year=2026/week=53/raw_battles.json"


def test_put_raw_battles_writes_body_metadata_and_tags(s3):
    storage = RawStorage(MINIO, client=s3)
    stored = storage.put_raw_battles({"hello": "world"}, record_count=7, extracted_at=WHEN)

    assert stored.uri == f"s3://{BUCKET}/year=2026/week=45/raw_battles.json"
    head = s3.head_object(Bucket=BUCKET, Key=stored.key)
    assert head["ContentType"] == "application/json"
    assert head["Metadata"] == {"record-count": "7", "extracted-at": "2026-11-02T15:30:00+00:00"}
    tag_set = s3.get_object_tagging(Bucket=BUCKET, Key=stored.key)["TagSet"]
    tags = {t["Key"]: t["Value"] for t in tag_set}
    assert tags == {"record_count": "7", "extracted_at": "2026-11-02T15:30:00+00:00"}
    assert storage.get_json(stored.key) == {"hello": "world"}


class StubClient:
    base_url = "https://api.test/v1"

    def __init__(self, players, batch):
        self._players, self._batch = players, batch

    def get_leaderboard(self, limit, now=None):
        return Leaderboard("/live", None, self._players[:limit])

    def fetch_battle_logs(self, tags, max_workers=8):
        return self._batch


def test_run_extraction_end_to_end(s3):
    players = [RankedPlayer("#A", "a", 1), RankedPlayer("#B", "b", 2), RankedPlayer("#C", "c", 3)]
    batch = BattleLogBatch(
        logs={"#C": [{"x": 3}], "#A": [{"x": 1}, {"x": 2}]}, failures={"#B": "HTTP 404"}
    )
    summary = run_extraction(
        client=StubClient(players, batch), storage=RawStorage(MINIO, client=s3), now=WHEN
    )
    assert summary["players_succeeded"] == 2
    assert summary["players_failed"] == 1
    assert summary["battle_count"] == 3
    assert summary["key"] == "year=2026/week=45/raw_battles.json"

    payload = RawStorage(MINIO, client=s3).get_json(summary["key"])
    assert [b["player_tag"] for b in payload["battle_logs"]] == ["#A", "#C"]  # rank order
    assert payload["failures"] == [{"player_tag": "#B", "error": "HTTP 404"}]
    assert payload["leaderboard"][0] == {"tag": "#A", "name": "a", "rank": 1}
    assert (payload["year"], payload["week"]) == (2026, 45)
    assert payload["leaderboard_source"] == "/live"


def test_run_extraction_fails_when_every_log_fails():
    batch = BattleLogBatch(failures={"#A": "boom"})
    with pytest.raises(ExtractionError):
        run_extraction(client=StubClient([RankedPlayer("#A", "a", 1)], batch), upload=False)


def test_run_extraction_fails_on_empty_leaderboard():
    with pytest.raises(ExtractionError):
        run_extraction(client=StubClient([], BattleLogBatch()), upload=False)
