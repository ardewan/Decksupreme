"""Task 2a: filter raw battle logs down to standard 1v1 ladder games and parse them.

What counts as "standard competitive ladder": a 1v1 game played with the
player's own collection deck in Path of Legend (ranked) or Trophy Road (ladder).
Everything else is discarded: friendlies, clan-mate games, 2v2, tournaments,
river race / boat battles, challenges, draft and other special modes. The
allowlist is explicit, so a mode Supercell adds later is dropped until someone
opts it in here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

# (battle["type"], battle["gameMode"]["name"]) pairs that count as ladder. The
# Path of Legend arena modes are versioned (Ranked1v1_NewArena, ..._NewArena2),
# so they match by prefix.
LADDER_TYPES = frozenset({"pathOfLegend", "PvP", "trail"})
LADDER_MODE_EXACT = frozenset({"Ladder"})
LADDER_MODE_PREFIXES = ("Ranked1v1",)
DECK_SIZE = 8

MATCH_COLUMNS = [
    "player_tag", "player_name", "player_rank", "opponent_tag", "opponent_name",
    "battle_time", "player_crowns", "opponent_crowns", "player_cards", "opponent_cards",
    "battle_type", "game_mode",
]


def battle_mode(battle: dict[str, Any]) -> tuple[str, str]:
    return battle.get("type", "?"), (battle.get("gameMode") or {}).get("name", "?")


def is_ladder_battle(battle: dict[str, Any]) -> bool:
    btype, mode = battle_mode(battle)
    if btype not in LADDER_TYPES:
        return False
    if mode not in LADDER_MODE_EXACT and not mode.startswith(LADDER_MODE_PREFIXES):
        return False
    if battle.get("deckSelection", "collection") != "collection":
        return False
    if battle.get("isHostedMatch") or battle.get("isLadderTournament"):
        return False
    return len(battle.get("team", [])) == 1 and len(battle.get("opponent", [])) == 1


def parse_battle_time(value: str) -> datetime:
    """'20261102T172139.000Z' -> aware UTC datetime."""
    return datetime.strptime(value, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)


def _cards(side: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": int(c["id"]),
            "name": c.get("name", ""),
            "elixir": c.get("elixirCost"),
            "rarity": c.get("rarity"),
        }
        for c in side.get("cards", [])
    ]


@dataclass
class ParseResult:
    matches: pd.DataFrame  # one row per battle-log entry (mirror duplicates still present)
    players: list[dict[str, Any]]  # dim_players rows
    raw_battles: int
    discarded_by_mode: Counter
    malformed: int

    @property
    def ladder_battles(self) -> int:
        return len(self.matches)


def iter_battles(payload: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for entry in payload.get("battle_logs", []):
        for battle in entry.get("battles", []):
            yield entry["player_tag"], battle


def parse_players(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """dim_players rows from the leaderboard snapshot."""
    updated = datetime.fromisoformat(payload["extracted_at"])
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return [
        {
            "player_tag": p["tag"],
            "name": p.get("name"),
            "current_rank": p.get("rank"),
            "last_updated": updated,
        }
        for p in payload.get("leaderboard", [])
    ]


def parse_payload(payload: dict[str, Any]) -> ParseResult:
    ranks = {p["tag"]: p.get("rank") for p in payload.get("leaderboard", [])}
    rows: list[dict[str, Any]] = []
    discarded: Counter = Counter()
    malformed = 0
    raw = 0

    for owner_tag, battle in iter_battles(payload):
        raw += 1
        if not is_ladder_battle(battle):
            discarded["{}/{}".format(*battle_mode(battle))] += 1
            continue
        try:
            me, opp = battle["team"][0], battle["opponent"][0]
            row = {
                "player_tag": me["tag"],
                "player_name": me.get("name"),
                "player_rank": ranks.get(owner_tag),
                "opponent_tag": opp["tag"],
                "opponent_name": opp.get("name"),
                "battle_time": parse_battle_time(battle["battleTime"]),
                "player_crowns": int(me.get("crowns", 0)),
                "opponent_crowns": int(opp.get("crowns", 0)),
                "player_cards": _cards(me),
                "opponent_cards": _cards(opp),
                "battle_type": battle_mode(battle)[0],
                "game_mode": battle_mode(battle)[1],
            }
        except (KeyError, TypeError, ValueError):
            malformed += 1
            continue
        ok = (
            row["player_tag"] != row["opponent_tag"]
            and len(row["player_cards"]) == DECK_SIZE
            and len(row["opponent_cards"]) == DECK_SIZE
            and len({c["id"] for c in row["player_cards"]}) == DECK_SIZE
            and len({c["id"] for c in row["opponent_cards"]}) == DECK_SIZE
        )
        if not ok:
            malformed += 1
            continue
        rows.append(row)

    return ParseResult(
        matches=pd.DataFrame(rows, columns=MATCH_COLUMNS),
        players=parse_players(payload),
        raw_battles=raw,
        discarded_by_mode=discarded,
        malformed=malformed,
    )
