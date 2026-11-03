"""Tasks 4+5 against Postgres: SQL aggregates match hand counts; artifacts get written."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("sqlalchemy")

from decksupreme.db.upserts import upsert_cards  # noqa: E402
from decksupreme.load.pipeline import run_transform_and_load  # noqa: E402
from decksupreme.scoring.metrics import fetch_window_metrics  # noqa: E402
from decksupreme.scoring.pipeline import ScoringError, run_scoring_and_artifacts  # noqa: E402
from decksupreme.seeds.cards import load_card_seed  # noqa: E402
from tests.sample_payload import sample_payload  # noqa: E402

pytestmark = pytest.mark.integration


class FakeStorage:
    bucket = "test"

    def get_json(self, key):
        return sample_payload()


@pytest.fixture
def loaded(clean):
    upsert_cards(clean, load_card_seed())
    run_transform_and_load(2026, 45, storage=FakeStorage(), engine=clean)
    return clean


def test_aggregates_match_sample(loaded):
    w = fetch_window_metrics(loaded, week_id="2026-45")
    assert w.n_total == 5
    by = {c.name: c for c in w.cards}
    # Knight (DECK_A[0]) is in the player deck of all 5 kept matches; that side
    # won g1, lost g2, drew, won vs outsider, won trophy road -> 3/5.
    assert (by["Knight"].matches, by["Knight"].wins) == (5, 3)
    assert by["Knight"].ur == pytest.approx(5 / 10)
    # Golem (DECK_B) is on the opponent side of all 5; that side only won g2.
    assert by["Golem"].matches == 5
    assert by["Golem"].wins == 1
    assert by["Mirror"].ape is None


def test_scoring_pipeline_writes_artifacts(loaded, tmp_path):
    summary = run_scoring_and_artifacts(2026, 45, engine=loaded, out_dir=tmp_path, min_matches=1)
    assert summary["total_matches_analyzed"] == 5
    assert summary["total_cards_ranked"] == 16  # the sample's two full decks
    assert summary["unranked"] == {"Minion Giant": ["stats_pending"], "Mirror": ["variable_cost"]}
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest == json.loads((tmp_path / "rankings_2026_45.json").read_text())
    assert latest["cards"][0]["name"] == summary["top_card"]["name"]
    assert (tmp_path / "README.md").read_text().startswith("# DeckSupreme Card Rankings")


def test_default_min_matches_leaves_tiny_sample_unranked(loaded, tmp_path):
    with pytest.raises(ScoringError, match="min_matches=100"):
        run_scoring_and_artifacts(2026, 45, engine=loaded, out_dir=tmp_path, min_matches=100)


def test_scoring_empty_week_raises(loaded, tmp_path):
    with pytest.raises(ScoringError):
        run_scoring_and_artifacts(2026, 1, engine=loaded, out_dir=tmp_path)
