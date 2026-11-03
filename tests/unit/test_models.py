"""Schema shape tests: compile DDL for PostgreSQL without a live database."""

import pytest

sa = pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from decksupreme.db.models import Base, BridgeDeckCard, DimCard, DimPlayer, FactMatch  # noqa: E402

DIALECT = postgresql.dialect()


def ddl(model) -> str:
    return str(CreateTable(model.__table__).compile(dialect=DIALECT))


def index_names(model) -> set[str]:
    return {ix.name for ix in model.__table__.indexes}


def test_all_star_schema_tables_registered():
    assert set(Base.metadata.tables) == {
        "dim_cards", "dim_players", "fact_matches", "bridge_deck_cards",
    }


@pytest.mark.parametrize(
    "model, pk",
    [
        (DimCard, ["card_id"]),
        (DimPlayer, ["player_tag"]),
        (FactMatch, ["match_id"]),
        (BridgeDeckCard, ["match_id", "player_tag", "card_id"]),
    ],
)
def test_primary_keys(model, pk):
    assert [c.name for c in model.__table__.primary_key.columns] == pk


def test_spec_columns_present():
    cols = lambda m: set(m.__table__.c.keys())  # noqa: E731
    assert {"card_id", "name", "elixir", "base_hp", "base_dps", "speed_score"} <= cols(DimCard)
    assert {"player_tag", "name", "current_rank", "last_updated"} <= cols(DimPlayer)
    assert {"match_id", "battle_time", "player_tag", "opponent_tag",
            "winner_tag", "week_id"} <= cols(FactMatch)
    assert {"match_id", "player_tag", "card_id", "is_winner"} == cols(BridgeDeckCard)


def test_card_id_is_not_serial():
    assert "SERIAL" not in ddl(DimCard).upper()


def test_required_indexes():
    assert {"ix_fact_matches_battle_time", "ix_fact_matches_week_id"} <= index_names(FactMatch)
    assert "ix_bridge_deck_cards_card_id" in index_names(BridgeDeckCard)


def test_card_index_covers_is_winner():
    indexes = BridgeDeckCard.__table__.indexes
    (ix,) = [i for i in indexes if i.name == "ix_bridge_deck_cards_card_id"]
    assert "INCLUDE (is_winner)" in str(CreateIndex(ix).compile(dialect=DIALECT))


def test_bridge_foreign_keys():
    fks = {fk.parent.name: fk for fk in BridgeDeckCard.__table__.foreign_keys}
    assert fks["match_id"].target_fullname == "fact_matches.match_id"
    assert fks["match_id"].ondelete == "CASCADE"
    assert fks["card_id"].target_fullname == "dim_cards.card_id"
    assert "player_tag" not in fks  # opponents are outside dim_players


def test_timestamps_are_timezone_aware():
    assert FactMatch.__table__.c.battle_time.type.timezone is True
    assert DimPlayer.__table__.c.last_updated.type.timezone is True


def test_check_constraints_compiled():
    text = ddl(FactMatch)
    assert "winner_tag IS NULL OR winner_tag IN (player_tag, opponent_tag)" in text
    assert "ck_fact_matches_week_id_format" in text
    assert "speed_score IS NULL OR speed_score BETWEEN 0 AND 4" in ddl(DimCard)
