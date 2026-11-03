"""Task 2: filtering, deterministic match ids, mirror dedup, row mapping (no DB)."""

from __future__ import annotations

import copy
import hashlib
import random

import pytest

from decksupreme.transform.dedup import dedupe_matches, match_id
from decksupreme.transform.mapper import (
    BRIDGE_ROWS_PER_MATCH,
    check_cards,
    to_bridge_rows,
    to_fact_rows,
    week_id,
)
from decksupreme.transform.parser import is_ladder_battle, parse_payload
from tests.sample_payload import DECK_A, NAMES, battle, mirror, sample_payload

KNOWN = dict(NAMES)  # dim_cards stand-in: the 16 sample deck cards


# -- match id --------------------------------------------------------------
def test_match_id_matches_spec_formula():
    expected = hashlib.sha256(b"#AAA:#BBB:20261101T100000.000Z").hexdigest()
    assert match_id("#AAA", "#BBB", "20261101T100000.000Z") == expected


def test_match_id_is_symmetric_and_time_sensitive():
    t = "20261101T100000.000Z"
    assert match_id("#AAA", "#BBB", t) == match_id("#BBB", "#AAA", t)
    assert match_id("#AAA", "#BBB", t) != match_id("#AAA", "#BBB", "20261101T100001.000Z")
    assert match_id("#AAA", "#BBB", t) != match_id("#AAA", "#CCC", t)
    assert len(match_id("#AAA", "#BBB", t)) == 64


# -- filtering -------------------------------------------------------------
@pytest.mark.parametrize(
    ("btype", "mode", "keep"),
    [
        ("pathOfLegend", "Ranked1v1_NewArena", True),
        ("pathOfLegend", "Ranked1v1_NewArena2", True),
        ("trail", "Ladder", True),
        ("PvP", "Ladder", True),
        ("friendly", "Friendly", False),
        ("clanMate", "Friendly", False),
        ("trail", "Challenge_AllCards_EventDeck_NoSet", False),
        ("tournament", "Draft_Competitive", False),
        ("riverRacePvP", "CW_Battle_1v1", False),
        ("boatBattle", "ClanWar_BoatBattle", False),
        ("trail", "Chaos_1v1_Draft", False),
    ],
)
def test_mode_filter(btype, mode, keep):
    b = battle("#A", "#B", "20261101T100000.000Z", 1, 0, btype=btype, mode=mode)
    assert is_ladder_battle(b) is keep


def test_filter_drops_2v2_and_draft_deck_selection():
    two = battle(
        "#A", "#B", "20261101T100000.000Z", 1, 0, btype="trail", mode="Ladder", team_size=2
    )
    assert not is_ladder_battle(two)
    draft = battle("#A", "#B", "20261101T100000.000Z", 1, 0)
    draft["deckSelection"] = "draft"
    assert not is_ladder_battle(draft)


def test_parse_payload_counts():
    parsed = parse_payload(sample_payload())
    assert parsed.raw_battles == 11
    assert parsed.ladder_battles == 7  # 4 mirror copies + draw + outsider + trophy road
    assert sum(parsed.discarded_by_mode.values()) == 4
    assert parsed.malformed == 0
    assert [p["player_tag"] for p in parsed.players] == ["#AAA", "#BBB", "#CCC"]
    assert parsed.players[0]["current_rank"] == 1
    assert parsed.players[0]["last_updated"].isoformat() == "2026-11-02T15:30:00+00:00"


def test_malformed_deck_is_dropped():
    p = sample_payload()
    p["battle_logs"][0]["battles"][0]["team"][0]["cards"].pop()  # 7 cards
    parsed = parse_payload(p)
    assert parsed.malformed == 1
    assert parsed.ladder_battles == 6


# -- dedup -----------------------------------------------------------------
def test_mirror_matches_collapse_to_one():
    result = dedupe_matches(parse_payload(sample_payload()).matches)
    assert result.input_rows == 7
    assert result.collisions == 2
    assert result.unique == 5
    assert result.matches["match_id"].is_unique


def test_dedup_keeps_higher_ranked_players_copy():
    m = dedupe_matches(parse_payload(sample_payload()).matches).matches
    aaa_bbb = m[m["opponent_tag"].isin(["#AAA", "#BBB"]) & m["player_tag"].isin(["#AAA", "#BBB"])]
    assert len(aaa_bbb) == 2
    assert set(aaa_bbb["player_tag"]) == {"#AAA"}  # rank 1 beats rank 2


def test_dedup_is_independent_of_input_order():
    base = sample_payload()
    want = dedupe_matches(parse_payload(base).matches).matches
    rng = random.Random(7)
    for _ in range(5):
        shuffled = copy.deepcopy(base)
        rng.shuffle(shuffled["battle_logs"])
        for entry in shuffled["battle_logs"]:
            rng.shuffle(entry["battles"])
        got = dedupe_matches(parse_payload(shuffled).matches).matches
        assert got[["match_id", "player_tag"]].equals(want[["match_id", "player_tag"]])


def test_dedup_handles_many_collisions():
    p = sample_payload()
    g = battle("#AAA", "#BBB", "20261101T230000.000Z", 1, 0)
    p["battle_logs"][0]["battles"] += [g, copy.deepcopy(g)]  # same log twice (API overlap)
    p["battle_logs"][1]["battles"].append(mirror(g))
    result = dedupe_matches(parse_payload(p).matches)
    assert result.collisions == 2 + 2
    assert result.unique == 6


def test_dedup_empty():
    p = sample_payload()
    for e in p["battle_logs"]:
        e["battles"] = []
    result = dedupe_matches(parse_payload(p).matches)
    assert (result.unique, result.collisions) == (0, 0)


# -- mapping ---------------------------------------------------------------
def _mapped():
    m = dedupe_matches(parse_payload(sample_payload()).matches).matches
    return m, to_fact_rows(m, week_id(2026, 45)), to_bridge_rows(m)


def test_week_id_format():
    assert week_id(2026, 5) == "2026-05"


def test_fact_rows_winners_and_draws():
    _, facts, _ = _mapped()
    assert len(facts) == 5
    by_time = {f["battle_time"].strftime("%H"): f for f in facts}
    assert by_time["10"]["winner_tag"] == "#AAA"
    assert by_time["11"]["winner_tag"] == "#BBB"
    assert by_time["12"]["winner_tag"] is None  # draw
    assert {f["week_id"] for f in facts} == {"2026-45"}
    assert all(f["battle_time"].tzinfo is not None for f in facts)


def test_bridge_has_16_rows_per_match_with_correct_winner_flags():
    m, facts, bridge = _mapped()
    assert len(bridge) == BRIDGE_ROWS_PER_MATCH * len(facts)
    winners = {f["match_id"]: f["winner_tag"] for f in facts}
    for mid in winners:
        rows = [r for r in bridge if r["match_id"] == mid]
        assert len(rows) == 16
        assert len({r["player_tag"] for r in rows}) == 2
        for r in rows:
            assert r["is_winner"] == (r["player_tag"] == winners[mid])
    keys = {(r["match_id"], r["player_tag"], r["card_id"]) for r in bridge}
    assert len(keys) == len(bridge)  # bridge PK is unique


def test_check_cards_builds_placeholders_for_unknown_ids():
    m, _, _ = _mapped()
    check = check_cards(m, KNOWN)
    by_id = {c["card_id"]: c for c in check.placeholders}
    assert set(by_id) == {26000107, 28000006}
    assert by_id[26000107] == {
        "card_id": 26000107, "name": "Minion Giant", "elixir": 5, "card_type": "troop",
        "rarity": "epic", "unit_count": 1, "base_hp": None, "base_dps": None, "speed_score": None,
    }
    assert by_id[28000006]["elixir"] is None  # Mirror: variable cost
    assert by_id[28000006]["card_type"] == "spell"
    assert check.name_mismatches == {}


def test_check_cards_reports_name_mismatch():
    m, _, _ = _mapped()
    known = {**KNOWN, DECK_A[0]: "Sir Knight"}
    check = check_cards(m, known)
    assert check.name_mismatches == {DECK_A[0]: ("Knight", "Sir Knight")}
