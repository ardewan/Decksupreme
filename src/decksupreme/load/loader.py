"""Task 3: chunked bulk upserts into the warehouse (INSERT ... ON CONFLICT).

Everything for one weekly batch runs in a single transaction (load_batch), so a
failure leaves the warehouse exactly as it was. Each table is written in
multi-row INSERT chunks of `chunk_size`. RETURNING tells us how many rows were
actually new, which makes reruns observable: a second load of the same week
inserts 0 matches.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Table, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from decksupreme.db.models import BridgeDeckCard, DimCard, DimPlayer, FactMatch

DEFAULT_CHUNK = 2000


def _chunks(rows: Sequence[Mapping[str, Any]], size: int) -> Iterator[Sequence[Mapping[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def known_cards(bind: Engine | Connection) -> dict[int, str]:
    t = DimCard.__table__
    if isinstance(bind, Engine):
        with bind.connect() as conn:
            return known_cards(conn)
    return {cid: name for cid, name in bind.execute(select(t.c.card_id, t.c.name))}


def _insert_do_nothing(
    conn: Connection, table: Table, rows: Sequence[Mapping[str, Any]], key: Sequence[str],
    chunk_size: int,
) -> int:
    """INSERT ... ON CONFLICT (key) DO NOTHING; returns rows actually inserted."""
    inserted = 0
    for chunk in _chunks(rows, chunk_size):
        stmt = (
            insert(table)
            .values(list(chunk))
            .on_conflict_do_nothing(index_elements=list(key))
            .returning(literal_column("1"))
        )
        inserted += len(conn.execute(stmt).fetchall())
    return inserted


def insert_placeholder_cards(
    conn: Connection, rows: Sequence[Mapping[str, Any]], chunk_size: int = DEFAULT_CHUNK
) -> int:
    # DO NOTHING: never overwrite a curated seed row with battle-log guesses.
    return _insert_do_nothing(conn, DimCard.__table__, rows, ["card_id"], chunk_size)


def upsert_players(
    conn: Connection, rows: Sequence[Mapping[str, Any]], chunk_size: int = DEFAULT_CHUNK
) -> int:
    t = DimPlayer.__table__
    for chunk in _chunks(rows, chunk_size):
        stmt = insert(t).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[t.c.player_tag],
            set_={
                "name": stmt.excluded.name,
                "current_rank": stmt.excluded.current_rank,
                "last_updated": stmt.excluded.last_updated,
            },
        )
        conn.execute(stmt)
    return len(rows)


def insert_matches(
    conn: Connection, rows: Sequence[Mapping[str, Any]], chunk_size: int = DEFAULT_CHUNK
) -> int:
    return _insert_do_nothing(conn, FactMatch.__table__, rows, ["match_id"], chunk_size)


def insert_bridge(
    conn: Connection, rows: Sequence[Mapping[str, Any]], chunk_size: int = DEFAULT_CHUNK
) -> int:
    return _insert_do_nothing(
        conn, BridgeDeckCard.__table__, rows, ["match_id", "player_tag", "card_id"], chunk_size
    )


@dataclass(frozen=True)
class LoadResult:
    players_upserted: int
    placeholder_cards_inserted: int
    matches_inserted: int
    matches_already_present: int
    bridge_rows_inserted: int
    bridge_rows_already_present: int


def load_batch(
    engine: Engine,
    *,
    players: Sequence[Mapping[str, Any]],
    placeholder_cards: Sequence[Mapping[str, Any]],
    facts: Sequence[Mapping[str, Any]],
    bridge: Sequence[Mapping[str, Any]],
    chunk_size: int = DEFAULT_CHUNK,
) -> LoadResult:
    """One transaction. Order matters: cards before bridge (FK), facts before bridge."""
    with engine.begin() as conn:
        cards = insert_placeholder_cards(conn, placeholder_cards, chunk_size)
        n_players = upsert_players(conn, players, chunk_size)
        m = insert_matches(conn, facts, chunk_size)
        b = insert_bridge(conn, bridge, chunk_size)
    return LoadResult(n_players, cards, m, len(facts) - m, b, len(bridge) - b)
