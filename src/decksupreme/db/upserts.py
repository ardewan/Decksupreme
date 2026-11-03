"""Bulk upserts into the warehouse (PostgreSQL-specific INSERT ... ON CONFLICT)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import func, literal_column
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from decksupreme.db.models import DimCard
from decksupreme.seeds.cards import CardSeed

_CARD_UPDATE_COLS: Sequence[str] = (
    "name", "elixir", "base_hp", "base_dps",
    "speed_score", "card_type", "rarity", "unit_count",
)


@dataclass(frozen=True)
class UpsertResult:
    inserted: int
    updated: int
    unchanged: int

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


def upsert_cards(bind: Engine | Connection, cards: Iterable[CardSeed]) -> UpsertResult:
    """Upsert cards into dim_cards via ON CONFLICT (card_id) DO UPDATE.

    Rows whose values are identical are left untouched (no updated_at churn),
    which keeps re-seeding idempotent and the result counts meaningful.
    """
    rows = [c.db_row() for c in cards]
    if not rows:
        return UpsertResult(0, 0, 0)

    stmt = insert(DimCard.__table__).values(rows)
    excluded = stmt.excluded
    table = DimCard.__table__
    changed = None
    for col in _CARD_UPDATE_COLS:
        cond = table.c[col].is_distinct_from(excluded[col])
        changed = cond if changed is None else changed | cond

    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.card_id],
        set_={**{c: excluded[c] for c in _CARD_UPDATE_COLS}, "updated_at": func.now()},
        where=changed,
    ).returning(literal_column("(xmax = 0)").label("inserted"))
    # xmax = 0 identifies a freshly inserted tuple; updated tuples carry the
    # updating transaction id. Skipped (unchanged) rows return nothing.

    def _run(conn: Connection) -> UpsertResult:
        flags = [row.inserted for row in conn.execute(stmt)]
        inserted = sum(flags)
        updated = len(flags) - inserted
        return UpsertResult(inserted, updated, len(rows) - len(flags))

    if isinstance(bind, Connection):
        return _run(bind)
    with bind.begin() as conn:
        return _run(conn)
