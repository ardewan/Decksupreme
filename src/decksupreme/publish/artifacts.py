"""Task 5: JSON artifacts (latest + weekly archive) and the Markdown rankings report.

Files written to the rankings directory:
* latest.json               newest rankings (overwritten each run)
* rankings_YYYY_WW.json     immutable weekly archive, also the baseline for the
                            next week's risers/fallers
* README.md                 human-readable report
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from decksupreme.scoring.ranker import RankedCard

SCHEMA_VERSION = 1
DEFAULT_RANKINGS_DIR = Path(__file__).resolve().parents[3] / "rankings"
FEATURE_LABELS = {
    "wr": "Win rate", "ur": "Usage rate", "ape": "Attack/elixir",
    "dpe": "Defense/elixir", "ss": "Speed",
}


class ArtifactSchemaError(ValueError):
    pass


def archive_name(year: int, week: int) -> str:
    return f"rankings_{year:04d}_{week:02d}.json"


def previous_iso_week(year: int, week: int) -> tuple[int, int]:
    prev = date.fromisocalendar(year, week, 1) - timedelta(days=7)
    y, w, _ = prev.isocalendar()
    return y, w


# -- JSON ------------------------------------------------------------------
def build_payload(
    *,
    week_id: str,
    generated_at: datetime,
    total_matches: int,
    weights: Mapping[str, float],
    min_matches: int,
    ranked: Sequence[RankedCard],
) -> dict[str, Any]:
    return {
        "metadata": {
            "schema_version": SCHEMA_VERSION,
            "week_id": week_id,
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "total_matches_analyzed": total_matches,
            "total_cards_ranked": len(ranked),
            "weights_used": {k: weights[k] for k in FEATURE_LABELS},
            "min_matches": min_matches,
        },
        "cards": [r.to_dict() for r in ranked],
    }


_META_TYPES = {
    "schema_version": int, "week_id": str, "generated_at": str,
    "total_matches_analyzed": int, "total_cards_ranked": int, "weights_used": dict,
    "min_matches": int,
}
_CARD_TYPES = {
    "rank": int, "card_id": int, "name": str, "composite_score": (int, float),
    "raw": dict, "normalized": dict, "missing_features": list,
}
_METRIC_KEYS = {
    "win_rate", "usage_rate", "attack_per_elixir", "defense_per_elixir", "speed_score",
}


def validate_payload(payload: Mapping[str, Any]) -> None:
    """Structural check of the artifact schema. Raises ArtifactSchemaError."""
    problems: list[str] = []
    meta = payload.get("metadata")
    cards = payload.get("cards")
    if not isinstance(meta, dict):
        raise ArtifactSchemaError("metadata missing")
    if not isinstance(cards, list):
        raise ArtifactSchemaError("cards missing")
    for k, t in _META_TYPES.items():
        if not isinstance(meta.get(k), t):
            problems.append(f"metadata.{k} must be {t}")
    if isinstance(meta.get("weights_used"), dict) and not (
        abs(sum(meta["weights_used"].values()) - 1) < 1e-6
    ):
        problems.append("metadata.weights_used must sum to 1")
    if meta.get("total_cards_ranked") != len(cards):
        problems.append("metadata.total_cards_ranked != len(cards)")
    for i, c in enumerate(cards):
        for k, t in _CARD_TYPES.items():
            if not isinstance(c.get(k), t):
                problems.append(f"cards[{i}].{k} must be {t}")
        if c.get("rank") != i + 1:
            problems.append(f"cards[{i}].rank must be {i + 1}")
        if isinstance(c.get("normalized"), dict):
            if set(c["normalized"]) != _METRIC_KEYS:
                problems.append(f"cards[{i}].normalized keys wrong")
            for k, v in c["normalized"].items():
                if v is not None and not 0 <= v <= 1:
                    problems.append(f"cards[{i}].normalized.{k} outside [0, 1]")
        if isinstance(c.get("raw"), dict) and not set(c["raw"]) >= _METRIC_KEYS:
            problems.append(f"cards[{i}].raw keys wrong")
    if problems:
        raise ArtifactSchemaError("; ".join(problems[:10]))


def write_json_artifacts(
    payload: Mapping[str, Any], out_dir: Path, year: int, week: int
) -> list[Path]:
    validate_payload(payload)
    out_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    paths = [out_dir / "latest.json", out_dir / archive_name(year, week)]
    for p in paths:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)  # atomic: readers never see a half-written file
    return paths


def load_previous(out_dir: Path, year: int, week: int) -> dict[str, Any] | None:
    py, pw = previous_iso_week(year, week)
    path = out_dir / archive_name(py, pw)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# -- risers / fallers -----------------------------------------------------
def movers(
    current: Mapping[str, Any], previous: Mapping[str, Any] | None, n: int = 5
) -> dict[str, list[dict[str, Any]]]:
    """Rank changes for cards present in both weeks; positive delta = climbed."""
    if not previous:
        return {"risers": [], "fallers": []}
    before = {c["card_id"]: c["rank"] for c in previous.get("cards", [])}
    changes = [
        {"name": c["name"], "rank": c["rank"], "previous_rank": before[c["card_id"]],
         "delta": before[c["card_id"]] - c["rank"]}
        for c in current["cards"]
        if c["card_id"] in before
    ]
    risers = sorted((m for m in changes if m["delta"] > 0), key=lambda m: (-m["delta"], m["rank"]))
    fallers = sorted((m for m in changes if m["delta"] < 0), key=lambda m: (m["delta"], m["rank"]))
    return {"risers": risers[:n], "fallers": fallers[:n]}


# -- Markdown --------------------------------------------------------------
def _num(v: float | None, fmt: str) -> str:
    return "—" if v is None else format(v, fmt)


def render_markdown(
    payload: Mapping[str, Any], moves: Mapping[str, list[dict[str, Any]]], has_previous: bool
) -> str:
    meta = payload["metadata"]
    w = meta["weights_used"]
    lines = [
        f"# DeckSupreme Card Rankings — Week {meta['week_id']}",
        "",
        f"Generated **{meta['generated_at']}** from **{meta['total_matches_analyzed']:,}** "
        f"ladder matches played by the top 1,000 players. "
        f"{meta['total_cards_ranked']} cards ranked.",
        "",
        "Score = " + " + ".join(f"{w[k]:.2f}×{label}" for k, label in FEATURE_LABELS.items())
        + " (each min-max scaled to 0–1 across ranked cards).",
        "",
        "## Meta movers",
        "",
    ]
    if not has_previous:
        lines += ["_No previous week to compare against yet._", ""]
    else:
        for title, key, sign in (("Top risers", "risers", "▲"), ("Top fallers", "fallers", "▼")):
            lines += [f"**{title}**", ""]
            if moves[key]:
                lines += ["| Card | Rank | Change |", "|---|---:|---:|"]
                lines += [
                    f"| {m['name']} | {m['previous_rank']} → {m['rank']} "
                    f"| {sign} {abs(m['delta'])} |"
                    for m in moves[key]
                ]
            else:
                lines.append("_None this week._")
            lines.append("")

    lines += [
        "## Rankings",
        "",
        "| Rank | Card | Score | Win Rate | Usage Rate | Elixir | APE | DPE | Speed |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    flagged = False
    for c in payload["cards"]:
        raw = c["raw"]
        mark = ""
        if c["missing_features"]:
            mark, flagged = " \\*", True
        lines.append(
            f"| {c['rank']} | {c['name']}{mark} | {c['composite_score']:.3f} | "
            f"{raw['win_rate'] * 100:.1f}% | {raw['usage_rate'] * 100:.1f}% | "
            f"{c['elixir'] if c['elixir'] is not None else '—'} | "
            f"{_num(raw['attack_per_elixir'], '.1f')} | {_num(raw['defense_per_elixir'], '.1f')} | "
            f"{_num(raw['speed_score'], '.0f')} |"
        )
    lines.append("")
    if flagged:
        lines += [
            "\\* Card stats not in the seed yet (new card or variable cost). "
            "Scored on the known features only, with weights renormalised.",
            "",
        ]
    lines += [
        "_APE = attack DPS × units ÷ elixir (spells: total damage ÷ elixir). "
        "DPE = HP × units ÷ elixir. Usage rate = share of decks containing the card. "
        "Speed: 0 stationary/spell … 4 very fast._",
        "",
    ]
    return "\n".join(lines)


def write_markdown(markdown: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "README.md"
    path.write_text(markdown, encoding="utf-8")
    return path
