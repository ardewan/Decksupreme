"""Tasks 2 + 3 coordinator: raw JSON in MinIO -> filtered, deduplicated rows -> Postgres.

week_id on fact_matches is the *snapshot* week (the year/week of the raw
object), not each battle's own calendar week. A log of 25 battles taken on a
Monday mostly holds games from the week before, and Task 4 ranks "what the top
1,000 played in this snapshot". A match seen again in a later snapshot keeps
its first week_id (ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.engine import Engine

from decksupreme.extract.storage import RAW_BATTLES_FILENAME, RawStorage
from decksupreme.load.loader import DEFAULT_CHUNK, known_cards, load_batch
from decksupreme.transform.dedup import dedupe_matches
from decksupreme.transform.mapper import check_cards, to_bridge_rows, to_fact_rows, week_id
from decksupreme.transform.parser import parse_payload

log = logging.getLogger(__name__)


class TransformLoadError(RuntimeError):
    pass


def raw_key(year: int, week: int) -> str:
    return f"year={year:04d}/week={week:02d}/{RAW_BATTLES_FILENAME}"


def transform_payload(payload: dict[str, Any], year: int, week: int, known: dict[int, str]):
    """Pure (no I/O) part of the stage: returns rows ready for load_batch + stats."""
    parsed = parse_payload(payload)
    dedup = dedupe_matches(parsed.matches)
    cards = check_cards(dedup.matches, known)
    wid = week_id(year, week)
    facts = to_fact_rows(dedup.matches, wid)
    bridge = to_bridge_rows(dedup.matches)
    stats = {
        "week_id": wid,
        "raw_battles": parsed.raw_battles,
        "ladder_battles": parsed.ladder_battles,
        "discarded_non_ladder": sum(parsed.discarded_by_mode.values()),
        "discarded_malformed": parsed.malformed,
        "duplicate_collisions_dropped": dedup.collisions,
        "unique_matches": dedup.unique,
        "bridge_rows_built": len(bridge),
        "placeholder_cards": [f"{c['card_id']} {c['name']}" for c in cards.placeholders],
        "card_name_mismatches": {
            str(k): {"battle_log": a, "dim_cards": b} for k, (a, b) in cards.name_mismatches.items()
        },
        "top_discarded_modes": dict(parsed.discarded_by_mode.most_common(10)),
    }
    rows = {
        "players": parsed.players,
        "placeholder_cards": cards.placeholders,
        "facts": facts,
        "bridge": bridge,
    }
    return rows, stats


def run_transform_and_load(
    year: int,
    week: int,
    *,
    storage: RawStorage | None = None,
    engine: Engine | None = None,
    chunk_size: int = DEFAULT_CHUNK,
) -> dict[str, Any]:
    if engine is None:
        from decksupreme.db.session import get_engine

        engine = get_engine()
    storage = storage or RawStorage()
    key = raw_key(year, week)
    log.info("reading s3://%s/%s", storage.bucket, key)
    try:
        payload = storage.get_json(key)
    except Exception as exc:
        raise TransformLoadError(f"could not read raw object {key}: {exc}") from exc

    rows, stats = transform_payload(payload, year, week, known_cards(engine))
    log.info(
        "%d raw -> %d ladder -> %d unique matches (%d mirror duplicates dropped)",
        stats["raw_battles"], stats["ladder_battles"], stats["unique_matches"],
        stats["duplicate_collisions_dropped"],
    )
    for cid, m in stats["card_name_mismatches"].items():
        log.warning("card %s: battle log says %r, dim_cards says %r", cid, *m.values())
    if stats["placeholder_cards"]:
        log.info("adding placeholder dim_cards rows: %s", ", ".join(stats["placeholder_cards"]))
    if not rows["facts"]:
        raise TransformLoadError(f"no ladder matches in {key}")

    result = load_batch(engine, chunk_size=chunk_size, **rows)
    stats.update(
        source_key=key,
        players_upserted=result.players_upserted,
        placeholder_cards_inserted=result.placeholder_cards_inserted,
        unique_matches_loaded=result.matches_inserted,
        matches_already_in_warehouse=result.matches_already_present,
        bridge_rows_written=result.bridge_rows_inserted,
        bridge_rows_already_in_warehouse=result.bridge_rows_already_present,
    )
    log.info(
        "loaded %d new matches (%d already present), %d bridge rows",
        result.matches_inserted, result.matches_already_present, result.bridge_rows_inserted,
    )
    return stats
