"""Task 4a: per-card aggregates from the warehouse plus the raw scoring features.

Definitions (one "appearance" = one card in one deck in one match):
* N_total  = matches in the window (fact_matches rows)
* matches_c = deck appearances of card c (bridge rows). A card in both decks
  counts twice: once as a win and once as a loss, which is exactly right for
  win rate.
* wins_c   = appearances on the winning side (draws count as non-wins)
* WR_c     = wins_c / matches_c                  (0.0 when matches_c == 0)
* UR_c     = matches_c / (2 * N_total)           share of decks containing c
* APE_c    = base_dps * unit_count / elixir      seed stats are per unit; spells
* DPE_c    = base_hp  * unit_count / elixir      store total damage, unit_count 1
* SS_c     = speed_score

UR uses decks (2 per match) so it reads as a true percentage. Min-max scaling
makes the constant factor irrelevant to scores. A feature whose inputs are NULL
(placeholder cards, or Mirror with its variable cost) is None, meaning
"unknown". The ranker leaves it out instead of scoring it as zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

FEATURES = ("wr", "ur", "ape", "dpe", "ss")


@dataclass(frozen=True)
class CardMetrics:
    card_id: int
    name: str
    card_type: str
    rarity: str | None
    elixir: int | None
    matches: int
    wins: int
    wr: float
    ur: float
    ape: float | None
    dpe: float | None
    ss: float | None

    def feature(self, name: str) -> float | None:
        return getattr(self, name)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WindowMetrics:
    label: str  # week_id or "since..until"
    n_total: int  # matches in the window
    cards: list[CardMetrics]


def _per_elixir(stat: int | None, units: int | None, elixir: int | None) -> float | None:
    if stat is None or not elixir:
        return None
    return stat * (units or 1) / elixir


def compute_card_metrics(rows: Iterable[Mapping[str, Any]], n_total: int) -> list[CardMetrics]:
    """Pure: aggregate rows (card attrs + matches + wins) -> CardMetrics."""
    decks = 2 * n_total
    out = []
    for r in rows:
        matches = int(r["matches"] or 0)
        wins = int(r["wins"] or 0)
        out.append(
            CardMetrics(
                card_id=int(r["card_id"]),
                name=r["name"],
                card_type=r.get("card_type") or "troop",
                rarity=r.get("rarity"),
                elixir=r.get("elixir"),
                matches=matches,
                wins=wins,
                wr=wins / matches if matches else 0.0,
                ur=matches / decks if decks else 0.0,
                ape=_per_elixir(r.get("base_dps"), r.get("unit_count"), r.get("elixir")),
                dpe=_per_elixir(r.get("base_hp"), r.get("unit_count"), r.get("elixir")),
                ss=float(r["speed_score"]) if r.get("speed_score") is not None else None,
            )
        )
    return out


_CARD_SQL = """
SELECT c.card_id, c.name, c.card_type, c.rarity, c.elixir, c.base_hp, c.base_dps,
       c.unit_count, c.speed_score,
       count(*)                       AS matches,
       sum(CASE WHEN b.is_winner THEN 1 ELSE 0 END) AS wins
FROM bridge_deck_cards b
JOIN fact_matches f USING (match_id)
JOIN dim_cards c    USING (card_id)
WHERE {where}
GROUP BY c.card_id
"""


def fetch_window_metrics(
    bind: Engine | Connection,
    *,
    week_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> WindowMetrics:
    """Aggregate one snapshot week (week_id) or a battle_time range [since, until)."""
    if week_id is not None:
        where, params, label = "f.week_id = :week_id", {"week_id": week_id}, week_id
    elif since is not None and until is not None:
        where = "f.battle_time >= :since AND f.battle_time < :until"
        params = {"since": since, "until": until}
        label = f"{since.isoformat()}..{until.isoformat()}"
    else:
        raise ValueError("pass week_id, or both since and until")

    if isinstance(bind, Engine):
        with bind.connect() as conn:
            return fetch_window_metrics(conn, week_id=week_id, since=since, until=until)

    n_total = bind.execute(
        text(f"SELECT count(*) FROM fact_matches f WHERE {where}"), params
    ).scalar_one()
    rows = bind.execute(text(_CARD_SQL.format(where=where)), params).mappings().all()
    return WindowMetrics(label, int(n_total), compute_card_metrics(rows, int(n_total)))
