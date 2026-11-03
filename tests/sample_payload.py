"""Builds small raw payloads shaped like Phase 3's raw_battles.json, for tests."""

from __future__ import annotations

import copy
from typing import Any

# Eight real seeded card ids per deck (all present in data/cards_seed.json).
DECK_A = [26000000, 26000001, 26000002, 26000003, 26000004, 26000005, 26000006, 26000007]
DECK_B = [26000008, 26000009, 26000010, 26000011, 26000012, 26000013, 26000014, 26000015]
NEW_CARD = {"id": 26000107, "name": "Minion Giant", "elixirCost": 5, "rarity": "epic"}
MIRROR = {"id": 28000006, "name": "Mirror", "rarity": "epic"}  # no elixirCost in logs

NAMES = {
    26000000: "Knight", 26000001: "Archers", 26000002: "Goblins", 26000003: "Giant",
    26000004: "P.E.K.K.A", 26000005: "Minions", 26000006: "Balloon", 26000007: "Witch",
    26000008: "Barbarians", 26000009: "Golem", 26000010: "Skeletons", 26000011: "Valkyrie",
    26000012: "Skeleton Army", 26000013: "Bomber", 26000014: "Musketeer", 26000015: "Baby Dragon",
}


def card(cid: int) -> dict[str, Any]:
    return {"id": cid, "name": NAMES.get(cid, f"Card {cid}"), "elixirCost": 3, "rarity": "common"}


def side(tag: str, crowns: int, deck: list[Any]) -> dict[str, Any]:
    cards = [c if isinstance(c, dict) else card(c) for c in deck]
    return {"tag": tag, "name": tag.strip("#").lower(), "crowns": crowns, "cards": cards}


def battle(
    me: str, opp: str, time: str, my_crowns: int, opp_crowns: int,
    my_deck=DECK_A, opp_deck=DECK_B, btype="pathOfLegend", mode="Ranked1v1_NewArena2",
    team_size: int = 1,
) -> dict[str, Any]:
    return {
        "type": btype,
        "battleTime": time,
        "gameMode": {"id": 1, "name": mode},
        "deckSelection": "collection",
        "isHostedMatch": False,
        "isLadderTournament": False,
        "team": [side(me, my_crowns, my_deck)] * team_size,
        "opponent": [side(opp, opp_crowns, opp_deck)] * team_size,
    }


def mirror(b: dict[str, Any]) -> dict[str, Any]:
    """The same game as seen from the opponent's battle log."""
    m = copy.deepcopy(b)
    m["team"], m["opponent"] = m["opponent"], m["team"]
    return m


def sample_payload() -> dict[str, Any]:
    """3 top players. #AAA vs #BBB is played twice and appears in both logs
    (2 mirror collisions). Plus: a draw, a game vs an outsider using a new card
    and Mirror, a Trophy Road ladder game, and 4 non-ladder games to filter out."""
    g1 = battle("#AAA", "#BBB", "20261101T100000.000Z", 3, 1)
    g2 = battle("#AAA", "#BBB", "20261101T110000.000Z", 0, 1)
    draw = battle("#BBB", "#CCC", "20261101T120000.000Z", 1, 1)
    outsider = battle(
        "#CCC", "#ZZZ", "20261101T130000.000Z", 2, 0,
        my_deck=DECK_A[:6] + [NEW_CARD, MIRROR],
    )
    trophy_road = battle("#CCC", "#YYY", "20261101T140000.000Z", 1, 0, btype="trail", mode="Ladder")
    junk = [
        battle("#AAA", "#QQQ", "20261101T150000.000Z", 1, 0, btype="friendly", mode="Friendly"),
        battle("#AAA", "#QQQ", "20261101T160000.000Z", 1, 0, btype="clanMate2v2",
               mode="TeamVsTeam", team_size=2),
        battle("#BBB", "#QQQ", "20261101T170000.000Z", 1, 0, btype="trail",
               mode="Challenge_AllCards_EventDeck_NoSet"),
        battle("#BBB", "#QQQ", "20261101T180000.000Z", 1, 0, btype="tournament",
               mode="Draft_Competitive"),
    ]
    return {
        "schema_version": 1,
        "extracted_at": "2026-11-02T15:30:00+00:00",
        "year": 2026,
        "week": 45,
        "leaderboard": [
            {"tag": "#AAA", "name": "aaa", "rank": 1},
            {"tag": "#BBB", "name": "bbb", "rank": 2},
            {"tag": "#CCC", "name": "ccc", "rank": 3},
        ],
        "battle_logs": [
            {"player_tag": "#AAA", "battles": [g1, g2, junk[0], junk[1]]},
            {"player_tag": "#BBB", "battles": [mirror(g1), mirror(g2), draw, junk[2], junk[3]]},
            {"player_tag": "#CCC", "battles": [outsider, trophy_road]},
        ],
        "failures": [],
    }
