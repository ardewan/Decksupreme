"""Task 2c: map deduplicated matches to warehouse rows and check cards against dim_cards.

* fact_matches: one row per match. player_tag is whoever's log the kept copy
  came from (see dedup), and winner_tag is NULL for a draw.
* bridge_deck_cards: exactly 16 rows per match (8 per side). On a draw,
  is_winner is False for both sides.
* Cards are matched to dim_cards by id. A card the table doesn't know yet (a new
  release, or Mirror, which has no fixed cost) gets a placeholder dim_cards row
  built from the battle log, with NULL stats. A known id whose name differs is
  reported as a mismatch and still loaded, because the id is authoritative.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

BRIDGE_ROWS_PER_MATCH = 16

# Supercell id ranges: 26xxxxxx troops, 27xxxxxx buildings, 28xxxxxx spells.
_TYPE_BY_PREFIX = {26: "troop", 27: "building", 28: "spell"}
_RARITIES = {"common", "rare", "epic", "legendary", "champion"}


def week_id(year: int, week: int) -> str:
    return f"{year:04d}-{week:02d}"


def _winner(row: Any) -> str | None:
    if row.player_crowns > row.opponent_crowns:
        return row.player_tag
    if row.opponent_crowns > row.player_crowns:
        return row.opponent_tag
    return None


def to_fact_rows(matches: pd.DataFrame, week: str) -> list[dict[str, Any]]:
    return [
        {
            "match_id": r.match_id,
            "battle_time": pd.Timestamp(r.battle_time).to_pydatetime(),
            "player_tag": r.player_tag,
            "opponent_tag": r.opponent_tag,
            "winner_tag": _winner(r),
            "week_id": week,
        }
        for r in matches.itertuples(index=False)
    ]


def to_bridge_rows(matches: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in matches.itertuples(index=False):
        winner = _winner(r)
        for tag, cards in ((r.player_tag, r.player_cards), (r.opponent_tag, r.opponent_cards)):
            rows.extend(
                {
                    "match_id": r.match_id,
                    "player_tag": tag,
                    "card_id": c["id"],
                    "is_winner": winner == tag,
                }
                for c in cards
            )
    if len(rows) != BRIDGE_ROWS_PER_MATCH * len(matches):
        raise AssertionError(
            f"expected {BRIDGE_ROWS_PER_MATCH * len(matches)} bridge rows, built {len(rows)}"
        )
    return rows


@dataclass
class CardCheck:
    placeholders: list[dict[str, Any]] = field(default_factory=list)  # new dim_cards rows
    name_mismatches: dict[int, tuple[str, str]] = field(default_factory=dict)  # id: (log, db)


def check_cards(matches: pd.DataFrame, known: Mapping[int, str]) -> CardCheck:
    """Compare every card in the matches with dim_cards ({card_id: name})."""
    seen: dict[int, dict[str, Any]] = {}
    for col in ("player_cards", "opponent_cards"):
        for deck in matches[col]:
            for c in deck:
                prev = seen.get(c["id"])
                if prev is None or (prev["elixir"] is None and c["elixir"] is not None):
                    seen[c["id"]] = c

    check = CardCheck()
    for cid, c in sorted(seen.items()):
        if cid in known:
            if known[cid] != c["name"]:
                check.name_mismatches[cid] = (c["name"], known[cid])
            continue
        elixir = c["elixir"] if isinstance(c["elixir"], int) and 1 <= c["elixir"] <= 10 else None
        rarity = (c.get("rarity") or "").lower()
        check.placeholders.append(
            {
                "card_id": cid,
                "name": c["name"] or f"Unknown {cid}",
                "elixir": elixir,
                "card_type": _TYPE_BY_PREFIX.get(cid // 1_000_000, "troop"),
                "rarity": rarity if rarity in _RARITIES else None,
                "unit_count": 1,
                "base_hp": None,
                "base_dps": None,
                "speed_score": None,
            }
        )
    return check
