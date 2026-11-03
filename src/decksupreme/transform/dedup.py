"""Task 2b: deterministic match ids and in-memory mirror-match deduplication.

When two top-1,000 players meet, the game shows up in both battle logs: once
from each side, with team/opponent swapped. Hashing the ordered tag pair plus
the battle time gives both copies the same id. When both copies are present,
we keep the one from the higher-ranked player's log (tag order breaks a tie),
so the result never depends on the order the logs were fetched.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pandas as pd


def match_id(tag_a: str, tag_b: str, battle_time: str) -> str:
    """sha256 of 'min_tag:max_tag:battle_time'. Pass battle_time as the raw API
    string (e.g. '20261102T172139.000Z') so the id is reproducible from the source."""
    lo, hi = sorted((tag_a, tag_b))
    return hashlib.sha256(f"{lo}:{hi}:{battle_time}".encode()).hexdigest()


def battle_time_key(ts: pd.Timestamp) -> str:
    """Datetime back to the API's battleTime format, the canonical hash input."""
    return ts.strftime("%Y%m%dT%H%M%S.") + f"{ts.microsecond // 1000:03d}Z"


@dataclass
class DedupResult:
    matches: pd.DataFrame
    input_rows: int
    collisions: int  # rows dropped because their match_id was already kept

    @property
    def unique(self) -> int:
        return len(self.matches)


def add_match_ids(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["match_id"] = [
        match_id(a, b, battle_time_key(pd.Timestamp(t)))
        for a, b, t in zip(out["player_tag"], out["opponent_tag"], out["battle_time"], strict=True)
    ]
    return out


def dedupe_matches(df: pd.DataFrame) -> DedupResult:
    if df.empty:
        empty = df.assign(match_id=pd.Series(dtype=str))
        return DedupResult(empty, 0, 0)
    with_ids = df if "match_id" in df.columns else add_match_ids(df)
    ordered = with_ids.assign(_rank=with_ids["player_rank"].fillna(10**9)).sort_values(
        ["match_id", "_rank", "player_tag"], kind="mergesort"
    )
    kept = ordered.drop_duplicates("match_id", keep="first").drop(columns="_rank")
    kept = kept.sort_values(["battle_time", "match_id"], kind="mergesort").reset_index(drop=True)
    assert kept["match_id"].is_unique
    return DedupResult(kept, len(df), len(df) - len(kept))
