"""Integration tests against the stack's PostgreSQL 16.

Each test session creates a throwaway database (decksupreme_test_<random>),
so the real warehouse is never touched. Run: `make up && make test-integration`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

sa = pytest.importorskip("sqlalchemy")

from sqlalchemy import inspect, select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from decksupreme.db.models import BridgeDeckCard, DimCard, FactMatch  # noqa: E402
from decksupreme.db.session import init_db, session_scope  # noqa: E402
from decksupreme.db.upserts import upsert_cards  # noqa: E402
from decksupreme.seeds.cards import load_card_seed  # noqa: E402

pytestmark = pytest.mark.integration


def test_server_is_postgres_16(engine):
    with engine.connect() as conn:
        assert conn.execute(text("SHOW server_version")).scalar().startswith("16")


def test_init_db_creates_tables_and_indexes(engine, schema):
    insp = inspect(engine)
    assert set(schema) <= set(insp.get_table_names())
    assert {"dim_cards", "dim_players", "fact_matches", "bridge_deck_cards"} <= set(schema)
    fact_ix = {ix["name"] for ix in insp.get_indexes("fact_matches")}
    assert {"ix_fact_matches_battle_time", "ix_fact_matches_week_id"} <= fact_ix
    bridge_ix = {ix["name"] for ix in insp.get_indexes("bridge_deck_cards")}
    assert "ix_bridge_deck_cards_card_id" in bridge_ix
    assert insp.get_pk_constraint("bridge_deck_cards")["constrained_columns"] == [
        "match_id", "player_tag", "card_id",
    ]


def test_init_db_is_idempotent(engine, schema):
    assert init_db(engine) == schema


def test_seed_inserts_then_is_idempotent(clean):
    cards = load_card_seed()
    first = upsert_cards(clean, cards)
    assert (first.inserted, first.updated, first.unchanged) == (len(cards), 0, 0)

    second = upsert_cards(clean, cards)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, len(cards))

    with clean.connect() as conn:
        assert conn.execute(select(sa.func.count()).select_from(DimCard)).scalar() == len(cards)


def test_seed_updates_changed_rows_on_conflict(clean):
    cards = load_card_seed()
    upsert_cards(clean, cards)
    knight = next(c for c in cards if c.name == "Knight")
    changed = [replace(knight, base_hp=knight.base_hp + 100)]

    result = upsert_cards(clean, changed)
    assert (result.inserted, result.updated, result.unchanged) == (0, 1, 0)
    with clean.connect() as conn:
        hp = conn.execute(select(DimCard.base_hp).where(DimCard.card_id == knight.card_id)).scalar()
    assert hp == knight.base_hp + 100


def test_seeded_values_round_trip(clean):
    cards = {c.card_id: c for c in load_card_seed()}
    upsert_cards(clean, cards.values())
    with session_scope(clean) as s:
        for row in s.execute(select(DimCard)).scalars():
            assert row.elixir == cards[row.card_id].elixir
            assert row.speed_score == cards[row.card_id].speed_score
            assert row.unit_count == cards[row.card_id].unit_count


def _match(**kw):
    base = dict(
        match_id="a" * 64,
        battle_time=datetime(2026, 11, 2, 12, tzinfo=UTC),
        player_tag="#P1", opponent_tag="#P2", winner_tag="#P1", week_id="2026-45",
    )
    return FactMatch(**{**base, **kw})


def test_fact_constraints_enforced(clean):
    for bad in (_match(winner_tag="#NOPE"), _match(opponent_tag="#P1"), _match(week_id="2026W45")):
        with pytest.raises(IntegrityError), session_scope(clean) as s:
            s.add(bad)


def test_bridge_requires_known_card_and_cascades(clean):
    upsert_cards(clean, load_card_seed())
    with session_scope(clean) as s:
        s.add(_match())
        s.flush()
        s.add(BridgeDeckCard(match_id="a" * 64, player_tag="#P1", card_id=26000000, is_winner=True))

    with pytest.raises(IntegrityError), session_scope(clean) as s:
        s.add(BridgeDeckCard(match_id="a" * 64, player_tag="#P1", card_id=1, is_winner=True))

    with session_scope(clean) as s:
        s.delete(s.get(FactMatch, "a" * 64))
    with clean.connect() as conn:
        assert conn.execute(select(sa.func.count()).select_from(BridgeDeckCard)).scalar() == 0
