"""Task 4b: min-max normalisation, weighted composite score, deterministic ranking.

* Each feature is min-max scaled to [0, 1] over the active cards whose value is
  known. If max == min, every known value scales to 0.0 (no divide-by-zero).
* S(c) = sum(w_f * norm_f(c)). When some features of a card are unknown
  (placeholder cards or Mirror), the score uses only its known features, with
  the weights renormalised to sum to 1, so missing data is left out rather than
  scored as zero. Those cards are flagged with `missing_features`.
* Order: S desc, WR desc, APE desc, DPE desc, SS desc, UR desc, name asc. An
  unknown value sorts below any known value. Scores compare after rounding to
  12 decimals, so float noise can't break a real tie.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from decksupreme.config import DEFAULT_WEIGHTS
from decksupreme.scoring.metrics import FEATURES, CardMetrics

SCORE_DECIMALS = 12


def min_max(values: Sequence[float | None]) -> list[float | None]:
    known = [v for v in values if v is not None]
    if not known:
        return [None] * len(values)
    lo, hi = min(known), max(known)
    span = hi - lo
    return [
        None if v is None else (0.0 if span == 0 else (v - lo) / span)
        for v in values
    ]


def validate_weights(weights: Mapping[str, float]) -> dict[str, float]:
    if set(weights) != set(FEATURES):
        raise ValueError(f"weights must cover exactly {FEATURES}, got {sorted(weights)}")
    if any(w < 0 for w in weights.values()) or not math.isclose(sum(weights.values()), 1.0):
        raise ValueError(f"weights must be non-negative and sum to 1.0, got {dict(weights)}")
    return {f: float(weights[f]) for f in FEATURES}


@dataclass(frozen=True)
class RankedCard:
    rank: int
    metrics: CardMetrics
    normalized: dict[str, float | None]
    score: float
    missing_features: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.metrics.name

    def to_dict(self) -> dict[str, Any]:
        m = self.metrics
        return {
            "rank": self.rank,
            "card_id": m.card_id,
            "name": m.name,
            "card_type": m.card_type,
            "rarity": m.rarity,
            "elixir": m.elixir,
            "composite_score": round(self.score, 6),
            "raw": {
                "matches": m.matches,
                "wins": m.wins,
                "win_rate": m.wr,
                "usage_rate": m.ur,
                "attack_per_elixir": m.ape,
                "defense_per_elixir": m.dpe,
                "speed_score": m.ss,
            },
            "normalized": {
                "win_rate": self.normalized["wr"],
                "usage_rate": self.normalized["ur"],
                "attack_per_elixir": self.normalized["ape"],
                "defense_per_elixir": self.normalized["dpe"],
                "speed_score": self.normalized["ss"],
            },
            "missing_features": self.missing_features,
        }


def composite(norm: Mapping[str, float | None], weights: Mapping[str, float]) -> float:
    known = {f: v for f, v in norm.items() if v is not None}
    total_w = sum(weights[f] for f in known)
    if total_w == 0:
        return 0.0
    return sum(weights[f] * v for f, v in known.items()) / total_w


def _desc(v: float | None) -> float:
    return -v if v is not None else math.inf  # unknown sorts last


def sort_key(card: CardMetrics, score: float) -> tuple:
    return (
        -round(score, SCORE_DECIMALS),
        _desc(card.wr),
        _desc(card.ape),
        _desc(card.dpe),
        _desc(card.ss),
        _desc(card.ur),
        card.name,
    )


def rank_cards(
    cards: Sequence[CardMetrics],
    weights: Mapping[str, float] | None = None,
    min_matches: int = 1,
) -> list[RankedCard]:
    w = validate_weights(weights or DEFAULT_WEIGHTS)
    active = [c for c in cards if c.matches >= max(min_matches, 1)]
    columns = {f: min_max([c.feature(f) for c in active]) for f in FEATURES}

    scored = []
    for i, c in enumerate(active):
        norm = {f: columns[f][i] for f in FEATURES}
        scored.append((c, norm, composite(norm, w)))
    scored.sort(key=lambda t: sort_key(t[0], t[2]))

    return [
        RankedCard(
            rank=pos,
            metrics=c,
            normalized=norm,
            score=s,
            missing_features=[f for f in FEATURES if norm[f] is None],
        )
        for pos, (c, norm, s) in enumerate(scored, start=1)
    ]
