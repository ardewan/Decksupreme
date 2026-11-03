"""Tasks 4 + 5 coordinator: warehouse aggregates -> ranking -> rankings/ artifacts."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from decksupreme.publish.artifacts import (
    DEFAULT_RANKINGS_DIR,
    build_payload,
    load_previous,
    movers,
    render_markdown,
    write_json_artifacts,
    write_markdown,
)
from decksupreme.scoring.metrics import fetch_window_metrics
from decksupreme.scoring.ranker import rank_cards, validate_weights
from decksupreme.transform.mapper import week_id as make_week_id

log = logging.getLogger(__name__)


class ScoringError(RuntimeError):
    pass


def run_scoring_and_artifacts(
    year: int,
    week: int,
    *,
    engine: Engine | None = None,
    out_dir: Path | None = None,
    weights: Mapping[str, float] | None = None,
    min_matches: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if engine is None:
        from decksupreme.db.session import get_engine

        engine = get_engine()
    if weights is None or min_matches is None:
        from decksupreme.config import get_settings

        scoring = get_settings().scoring
        weights = weights if weights is not None else scoring.weights
        min_matches = min_matches if min_matches is not None else scoring.min_matches
    weights = validate_weights(weights)
    out_dir = out_dir or DEFAULT_RANKINGS_DIR
    wid = make_week_id(year, week)

    window = fetch_window_metrics(engine, week_id=wid)
    if window.n_total == 0:
        raise ScoringError(f"no matches in the warehouse for week {wid}; run the load first")
    ranked = rank_cards(window.cards, weights, min_matches)
    if not ranked:
        raise ScoringError(f"no cards met min_matches={min_matches} in week {wid}")

    payload = build_payload(
        week_id=wid,
        generated_at=(now or datetime.now(UTC)),
        total_matches=window.n_total,
        weights=weights,
        min_matches=min_matches,
        ranked=ranked,
    )
    previous = load_previous(out_dir, year, week)
    moves = movers(payload, previous)
    files = write_json_artifacts(payload, out_dir, year, week)
    files.append(write_markdown(render_markdown(payload, moves, previous is not None), out_dir))
    log.info("ranked %d cards from %d matches -> %s", len(ranked), window.n_total, out_dir)

    top, bottom = ranked[0], ranked[-1]
    return {
        "week_id": wid,
        "total_matches_analyzed": window.n_total,
        "total_cards_ranked": len(ranked),
        "top_card": {"name": top.name, "score": round(top.score, 4)},
        "lowest_card": {"name": bottom.name, "score": round(bottom.score, 4)},
        "incomplete_cards": [r.name for r in ranked if r.missing_features],
        "compared_with_previous_week": previous is not None,
        "risers": moves["risers"],
        "fallers": moves["fallers"],
        "files": [str(p) for p in files],
    }
