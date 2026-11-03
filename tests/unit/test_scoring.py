"""Task 4/5: metrics, normalisation, scoring, tie-breaks, artifacts (no DB)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from decksupreme.config import DEFAULT_WEIGHTS, parse_weights
from decksupreme.publish.artifacts import (
    ArtifactSchemaError,
    archive_name,
    build_payload,
    load_previous,
    movers,
    previous_iso_week,
    render_markdown,
    validate_payload,
    write_json_artifacts,
)
from decksupreme.scoring.metrics import CardMetrics, compute_card_metrics
from decksupreme.scoring.ranker import composite, min_max, rank_cards, validate_weights

NOW = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)


def card(name, *, wr=0.5, ur=0.1, ape=10.0, dpe=100.0, ss=2.0, matches=100, cid=None, elixir=4):
    return CardMetrics(
        card_id=cid or abs(hash(name)) % 10**8, name=name, card_type="troop", rarity="common",
        elixir=elixir, matches=matches, wins=round(wr * matches), wr=wr, ur=ur,
        ape=ape, dpe=dpe, ss=ss,
    )


# -- raw metrics -----------------------------------------------------------
def test_compute_card_metrics_formulas():
    rows = [
        {"card_id": 1, "name": "Skeleton Army", "elixir": 3, "base_hp": 81, "base_dps": 81,
         "unit_count": 15, "speed_score": 3, "matches": 40, "wins": 22},
        {"card_id": 2, "name": "Fireball", "card_type": "spell", "elixir": 4, "base_hp": 0,
         "base_dps": 688, "unit_count": 1, "speed_score": 0, "matches": 10, "wins": 3},
        {"card_id": 3, "name": "Mirror", "elixir": None, "base_hp": None, "base_dps": None,
         "unit_count": 1, "speed_score": None, "matches": 0, "wins": 0},
    ]
    army, fireball, mirror = compute_card_metrics(rows, n_total=100)
    assert army.wr == pytest.approx(0.55)
    assert army.ur == pytest.approx(40 / 200)  # share of 200 decks
    assert army.ape == pytest.approx(81 * 15 / 3)  # per-unit stat x unit count
    assert army.dpe == pytest.approx(81 * 15 / 3)
    assert army.ss == 3.0
    assert fireball.ape == pytest.approx(172.0) and fireball.dpe == 0.0
    assert mirror.wr == 0.0  # zero matches: guarded
    assert (mirror.ape, mirror.dpe, mirror.ss) == (None, None, None)


def test_compute_card_metrics_zero_total():
    (m,) = compute_card_metrics([{"card_id": 1, "name": "X", "matches": 0, "wins": 0}], 0)
    assert (m.wr, m.ur) == (0.0, 0.0)


# -- normalisation ---------------------------------------------------------
def test_min_max_basic():
    assert min_max([2.0, 4.0, 6.0]) == [0.0, 0.5, 1.0]


def test_min_max_identical_bounds_returns_zero():
    assert min_max([3.0, 3.0, 3.0]) == [0.0, 0.0, 0.0]
    assert min_max([7.0]) == [0.0]


def test_min_max_ignores_unknowns():
    assert min_max([None, 1.0, 3.0]) == [None, 0.0, 1.0]
    assert min_max([None, None]) == [None, None]


def test_all_features_constant_gives_zero_scores():
    ranked = rank_cards([card("A"), card("B"), card("C")])
    assert [r.score for r in ranked] == [0.0, 0.0, 0.0]
    assert [r.name for r in ranked] == ["A", "B", "C"]  # falls through to name


# -- weights ---------------------------------------------------------------
def test_default_weights_sum_to_one():
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)
    assert validate_weights(DEFAULT_WEIGHTS) == DEFAULT_WEIGHTS


@pytest.mark.parametrize(
    "bad", [{"wr": 1.0}, {**DEFAULT_WEIGHTS, "wr": 0.5}, {**DEFAULT_WEIGHTS, "wr": -0.1, "ur": 0.7}]
)
def test_invalid_weights_rejected(bad):
    with pytest.raises(ValueError):
        validate_weights(bad)


def test_parse_weights_from_env_string():
    w = parse_weights("wr=0.4, ur=0.2, ape=0.2, dpe=0.1, ss=0.1")
    assert w == {"wr": 0.4, "ur": 0.2, "ape": 0.2, "dpe": 0.1, "ss": 0.1}
    assert parse_weights("") == DEFAULT_WEIGHTS
    with pytest.raises(ValueError):
        parse_weights("wr=1")


def test_composite_weighted_sum_and_missing_renormalisation():
    norm = {"wr": 1.0, "ur": 0.0, "ape": 0.5, "dpe": 0.5, "ss": 1.0}
    assert composite(norm, DEFAULT_WEIGHTS) == pytest.approx(0.35 + 0.075 + 0.075 + 0.10)
    partial = {"wr": 1.0, "ur": 0.0, "ape": None, "dpe": None, "ss": None}
    assert composite(partial, DEFAULT_WEIGHTS) == pytest.approx(0.35 / 0.60)


def test_scores_use_min_max_across_cards():
    ranked = rank_cards(
        [card("Low", wr=0.4, ur=0.1, ape=5, dpe=50, ss=1),
         card("High", wr=0.6, ur=0.3, ape=15, dpe=150, ss=4)]
    )
    assert [r.name for r in ranked] == ["High", "Low"]
    assert ranked[0].score == pytest.approx(1.0)
    assert ranked[1].score == 0.0
    assert ranked[0].normalized == {"wr": 1.0, "ur": 1.0, "ape": 1.0, "dpe": 1.0, "ss": 1.0}


def test_min_matches_excludes_inactive_cards():
    ranked = rank_cards([card("A"), card("Unused", matches=0), card("Rare", matches=5)],
                        min_matches=10)
    assert [r.name for r in ranked] == ["A"]


def test_card_with_unknown_features_is_flagged_not_zeroed():
    full = card("Full", wr=0.5)
    new = card("New", wr=0.6, ape=None, dpe=None, ss=None)
    ranked = {r.name: r for r in rank_cards([full, new, card("Base", wr=0.4)])}
    assert ranked["New"].missing_features == ["ape", "dpe", "ss"]
    assert ranked["New"].score > 0
    assert ranked["Full"].missing_features == []


# -- tie-breaking ----------------------------------------------------------
def _order(cards, weights):
    return [r.name for r in rank_cards(cards, weights)]


ONLY_UR = {"wr": 0.0, "ur": 1.0, "ape": 0.0, "dpe": 0.0, "ss": 0.0}


def test_tie_break_hierarchy_each_level():
    """Every card has the same UR and the weights only use UR, so all scores tie
    at 0 and each pair differs at exactly one tie-break level."""
    base = dict(ur=0.1, wr=0.5, ape=10.0, dpe=100.0, ss=2.0)
    cards = [
        card("Zed-best-wr", **{**base, "wr": 0.9}),
        card("Yan-best-ape", **{**base, "ape": 20.0}),
        card("Xia-best-dpe", **{**base, "dpe": 200.0}),
        card("Wes-best-ss", **{**base, "ss": 4.0}),
        card("Bea", **base),
        card("Ann", **base),  # name ASC breaks the final tie
    ]
    ranked = rank_cards(cards, ONLY_UR)
    assert {r.score for r in ranked} == {0.0}
    assert [r.name for r in ranked] == [
        "Zed-best-wr", "Yan-best-ape", "Xia-best-dpe", "Wes-best-ss", "Ann", "Bea",
    ]


def test_tie_break_usage_rate_level():
    # Weights only on SS; equal SS -> scores tie; UR is the 6th key.
    only_ss = {"wr": 0.0, "ur": 0.0, "ape": 0.0, "dpe": 0.0, "ss": 1.0}
    cards = [card("Aa", ur=0.1), card("Bb", ur=0.3)]
    assert _order(cards, only_ss) == ["Bb", "Aa"]


def test_tie_break_priority_order():
    # Higher WR outranks better APE/DPE/SS/UR when scores tie.
    a = card("A", wr=0.51, ape=1, dpe=1, ss=0, ur=0.1)
    b = card("B", wr=0.50, ape=99, dpe=999, ss=4, ur=0.1)
    assert _order([b, a], ONLY_UR) == ["A", "B"]


def test_unknown_values_sort_after_known():
    a = card("A", ape=None)
    b = card("B", ape=1.0)
    assert _order([a, b], ONLY_UR) == ["B", "A"]


def test_float_noise_does_not_break_ties():
    w = {"wr": 0.1, "ur": 0.2, "ape": 0.3, "dpe": 0.4, "ss": 0.0}
    cards = [card("B", ape=11, dpe=101), card("A", ape=11, dpe=101), card("Lo", ape=1, dpe=1)]
    assert _order(cards, w)[:2] == ["A", "B"]


def test_ranking_is_order_independent():
    cards = [card(n, wr=w, ur=u) for n, w, u in
             [("A", 0.5, 0.1), ("B", 0.6, 0.1), ("C", 0.5, 0.2), ("D", 0.5, 0.1)]]
    assert _order(cards, DEFAULT_WEIGHTS) == _order(list(reversed(cards)), DEFAULT_WEIGHTS)


# -- artifacts -------------------------------------------------------------
def _payload(cards=None, week="2026-45", now=NOW):
    ranked = rank_cards(cards or [
        card("Hog Rider", wr=0.55, ur=0.3, cid=1), card("Knight", wr=0.5, ur=0.2, cid=2),
        card("Mirror", wr=0.45, ur=0.05, ape=None, dpe=None, ss=None, cid=3, elixir=None),
    ])
    return build_payload(week_id=week, generated_at=now, total_matches=21273,
                         weights=DEFAULT_WEIGHTS, min_matches=1, ranked=ranked)


def test_payload_schema_is_valid():
    p = _payload()
    validate_payload(p)
    assert p["metadata"]["week_id"] == "2026-45"
    assert p["metadata"]["generated_at"] == "2026-11-02T18:00:00+00:00"
    assert p["metadata"]["total_matches_analyzed"] == 21273
    assert p["metadata"]["weights_used"] == DEFAULT_WEIGHTS
    first = p["cards"][0]
    assert first["rank"] == 1
    assert set(first["raw"]) >= {"win_rate", "usage_rate", "attack_per_elixir",
                                 "defense_per_elixir", "speed_score", "matches", "wins"}
    json.dumps(p)  # serialisable


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("metadata"),
        lambda p: p["metadata"].pop("week_id"),
        lambda p: p["metadata"].update(total_cards_ranked=99),
        lambda p: p["cards"][0].update(rank=2),
        lambda p: p["cards"][0]["normalized"].update(win_rate=1.5),
        lambda p: p["metadata"]["weights_used"].update(wr=0.9),
    ],
)
def test_schema_validation_rejects_bad_payloads(mutate):
    p = _payload()
    mutate(p)
    with pytest.raises(ArtifactSchemaError):
        validate_payload(p)


def test_write_json_artifacts(tmp_path):
    p = _payload()
    paths = write_json_artifacts(p, tmp_path, 2026, 45)
    assert [x.name for x in paths] == ["latest.json", "rankings_2026_45.json"]
    assert json.loads((tmp_path / "latest.json").read_text()) == p
    assert not list(tmp_path.glob("*.tmp"))


def test_previous_week_across_year_boundary():
    assert previous_iso_week(2026, 45) == (2026, 44)
    assert previous_iso_week(2027, 1) == (2026, 53)
    assert archive_name(2026, 5) == "rankings_2026_05.json"


def test_movers_against_previous_week(tmp_path):
    prev = _payload([card("Hog Rider", wr=0.4, cid=1), card("Knight", wr=0.6, cid=2),
                     card("Gone", wr=0.5, cid=9)], week="2026-44")
    write_json_artifacts(prev, tmp_path, 2026, 44)
    loaded = load_previous(tmp_path, 2026, 45)
    cur = _payload([card("Hog Rider", wr=0.7, cid=1), card("Knight", wr=0.3, cid=2),
                    card("Brand New", wr=0.5, cid=10)])
    m = movers(cur, loaded)
    assert m["risers"] == [{"name": "Hog Rider", "rank": 1, "previous_rank": 3, "delta": 2}]
    assert m["fallers"] == [{"name": "Knight", "rank": 3, "previous_rank": 1, "delta": -2}]
    assert load_previous(tmp_path, 2026, 40) is None
    assert movers(cur, None) == {"risers": [], "fallers": []}


def test_markdown_rendering():
    p = _payload()
    md = render_markdown(p, {"risers": [], "fallers": []}, has_previous=False)
    assert md.startswith("# DeckSupreme Card Rankings — Week 2026-45")
    assert "2026-11-02T18:00:00+00:00" in md and "21,273" in md
    assert "| Rank | Card | Score | Win Rate | Usage Rate | Elixir | APE | DPE | Speed |" in md
    rows = [ln for ln in md.splitlines() if ln.startswith("| ") and ln[2].isdigit()]
    assert len(rows) == 3
    assert rows[0].startswith("| 1 | Hog Rider |")
    assert "55.0%" in rows[0]
    mirror = next(r for r in rows if "Mirror" in r)
    assert "\\*" in mirror and "| — |" in mirror
    assert "No previous week" in md


def test_markdown_movers_section():
    p = _payload()
    moves = {"risers": [{"name": "Hog Rider", "rank": 1, "previous_rank": 4, "delta": 3}],
             "fallers": []}
    md = render_markdown(p, moves, has_previous=True)
    assert "| Hog Rider | 4 → 1 | ▲ 3 |" in md
    assert "_None this week._" in md
