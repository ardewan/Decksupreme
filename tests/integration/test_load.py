"""End-to-end Tasks 2+3 against the stack's Postgres (throwaway DB, see test_warehouse)."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text  # noqa: E402

from decksupreme.db.upserts import upsert_cards  # noqa: E402
from decksupreme.load.pipeline import TransformLoadError, run_transform_and_load  # noqa: E402
from decksupreme.seeds.cards import load_card_seed  # noqa: E402
from tests.sample_payload import sample_payload  # noqa: E402

pytestmark = pytest.mark.integration


class FakeStorage:
    bucket = "test"

    def __init__(self, payloads):
        self.payloads = payloads

    def get_json(self, key):
        return self.payloads[key]


KEY = "year=2026/week=45/raw_battles.json"


def _count(bind, sql):
    with bind.connect() as conn:
        return conn.execute(text(sql)).scalar()


def test_end_to_end_load_and_rerun_is_idempotent(clean):
    upsert_cards(clean, load_card_seed())
    storage = FakeStorage({KEY: sample_payload()})

    stats = run_transform_and_load(2026, 45, storage=storage, engine=clean, chunk_size=3)
    assert stats["unique_matches_loaded"] == 5
    assert stats["duplicate_collisions_dropped"] == 2
    assert stats["bridge_rows_written"] == 80
    assert stats["players_upserted"] == 3
    assert stats["placeholder_cards_inserted"] == 2

    assert _count(clean, "SELECT count(*) FROM fact_matches") == 5
    assert _count(clean, "SELECT count(*) FROM bridge_deck_cards") == 80
    assert _count(clean, "SELECT count(*) FROM fact_matches WHERE winner_tag IS NULL") == 1
    assert _count(clean, "SELECT count(DISTINCT week_id) FROM fact_matches") == 1
    assert _count(
        clean, "SELECT count(*) FROM (SELECT match_id FROM bridge_deck_cards "
        "GROUP BY match_id HAVING count(*) <> 16) x"
    ) == 0
    assert _count(
        clean, "SELECT elixir IS NULL AND base_hp IS NULL FROM dim_cards WHERE card_id = 28000006"
    ) is True
    assert _count(clean, "SELECT card_type FROM dim_cards WHERE card_id = 28000006") == "spell"

    again = run_transform_and_load(2026, 45, storage=storage, engine=clean)
    assert again["unique_matches_loaded"] == 0
    assert again["matches_already_in_warehouse"] == 5
    assert again["bridge_rows_written"] == 0
    assert again["placeholder_cards_inserted"] == 0
    assert _count(clean, "SELECT count(*) FROM fact_matches") == 5


def test_player_rank_is_updated_on_conflict(clean):
    upsert_cards(clean, load_card_seed())
    first = sample_payload()
    run_transform_and_load(2026, 45, storage=FakeStorage({KEY: first}), engine=clean)

    later = sample_payload()
    later["extracted_at"] = "2026-11-09T15:30:00+00:00"
    later["leaderboard"][0]["rank"] = 7
    later["leaderboard"][0]["name"] = "renamed"
    key46 = "year=2026/week=46/raw_battles.json"
    run_transform_and_load(2026, 46, storage=FakeStorage({key46: later}), engine=clean)

    with clean.connect() as conn:
        name, rank, ts = conn.execute(
            text("SELECT name, current_rank, last_updated FROM dim_players WHERE player_tag='#AAA'")
        ).one()
    assert (name, rank) == ("renamed", 7)
    assert ts.isoformat().startswith("2026-11-09T15:30")
    # same games re-seen in week 46 keep their week-45 rows
    assert _count(clean, "SELECT count(*) FROM fact_matches WHERE week_id = '2026-46'") == 0


def test_seed_rows_are_never_overwritten_by_placeholders(clean):
    upsert_cards(clean, load_card_seed())
    before = _count(clean, "SELECT base_hp FROM dim_cards WHERE card_id = 26000000")
    run_transform_and_load(2026, 45, storage=FakeStorage({KEY: sample_payload()}), engine=clean)
    assert _count(clean, "SELECT base_hp FROM dim_cards WHERE card_id = 26000000") == before


def test_missing_raw_object_raises(clean):
    with pytest.raises(TransformLoadError):
        run_transform_and_load(2026, 1, storage=FakeStorage({}), engine=clean)
