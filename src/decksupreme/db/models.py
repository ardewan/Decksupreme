"""Warehouse star schema.

Written in Column-style declarative so it runs unchanged on SQLAlchemy 1.4
(pinned by Airflow 2.x) and 2.0.

Design notes
------------
* Player tags in the fact/bridge tables are NOT foreign keys to dim_players:
  opponents are usually outside the top-1,000 leaderboard, so they never get a
  dim_players row.
* bridge_deck_cards.card_id IS a foreign key to dim_cards. The Phase 4 loader
  (decksupreme.load.loader) inserts placeholder dim_cards rows (id/name/elixir
  from the battle log, NULL stats) for cards released after the seed was
  written. NULL stats mean "unknown" and are excluded from scoring rather than
  scored as zero.
* card_type / rarity / unit_count extend the spec's dim_cards columns because
  the scoring formulas need them (swarm DPE = HP x count / elixir; spell APE
  uses total damage).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    func,
)
from sqlalchemy.orm import declarative_base

# Deterministic constraint/index names make Alembic autogenerate diffs stable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Base = declarative_base(metadata=MetaData(naming_convention=NAMING_CONVENTION))

CARD_TYPES = ("troop", "building", "spell")
RARITIES = ("common", "rare", "epic", "legendary", "champion")


class DimCard(Base):
    __tablename__ = "dim_cards"

    card_id = Column(Integer, primary_key=True, autoincrement=False)
    name = Column(String(64), nullable=False, unique=True)
    # Nullable for variable-cost cards (Mirror) and placeholders whose cost the
    # battle log omits; the CHECK below still applies to every non-NULL value.
    elixir = Column(Integer, comment="NULL = variable/unknown cost")
    base_hp = Column(Integer, comment="Level 11 hitpoints per unit; 0 for spells; NULL = unknown")
    base_dps = Column(Integer, comment="Level 11 DPS; spells store total damage per cast")
    speed_score = Column(Integer, comment="0 stationary/spell, 1 slow .. 4 very fast")
    card_type = Column(String(16), nullable=False, server_default="troop")
    rarity = Column(String(16))
    unit_count = Column(Integer, nullable=False, server_default="1")
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("elixir BETWEEN 1 AND 10", name="elixir_range"),
        CheckConstraint("base_hp IS NULL OR base_hp >= 0", name="base_hp_nonneg"),
        CheckConstraint("base_dps IS NULL OR base_dps >= 0", name="base_dps_nonneg"),
        CheckConstraint("speed_score IS NULL OR speed_score BETWEEN 0 AND 4", name="speed_range"),
        CheckConstraint("unit_count >= 1", name="unit_count_pos"),
        CheckConstraint(
            "card_type IN ('troop', 'building', 'spell')", name="card_type_valid"
        ),
    )

    def __repr__(self) -> str:
        return f"<DimCard {self.card_id} {self.name!r}>"


class DimPlayer(Base):
    __tablename__ = "dim_players"

    player_tag = Column(String(16), primary_key=True)
    name = Column(String(64))
    current_rank = Column(Integer)
    last_updated = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("current_rank IS NULL OR current_rank >= 1", name="rank_pos"),
    )


class FactMatch(Base):
    __tablename__ = "fact_matches"

    match_id = Column(String(64), primary_key=True, comment="sha256(min tag || max tag || time)")
    battle_time = Column(DateTime(timezone=True), nullable=False)
    player_tag = Column(String(16), nullable=False)
    opponent_tag = Column(String(16), nullable=False)
    winner_tag = Column(String(16), comment="NULL = draw")
    week_id = Column(String(7), nullable=False, comment="ISO year-week, YYYY-WW")

    __table_args__ = (
        Index("ix_fact_matches_battle_time", "battle_time"),
        Index("ix_fact_matches_week_id", "week_id"),
        CheckConstraint("player_tag <> opponent_tag", name="distinct_players"),
        CheckConstraint(
            "winner_tag IS NULL OR winner_tag IN (player_tag, opponent_tag)",
            name="winner_is_participant",
        ),
        CheckConstraint("week_id ~ '^[0-9]{4}-[0-9]{2}$'", name="week_id_format"),
    )


class BridgeDeckCard(Base):
    __tablename__ = "bridge_deck_cards"

    match_id = Column(
        String(64),
        ForeignKey("fact_matches.match_id", ondelete="CASCADE"),
        primary_key=True,
    )
    player_tag = Column(String(16), primary_key=True)
    card_id = Column(Integer, ForeignKey("dim_cards.card_id"), primary_key=True)
    is_winner = Column(Boolean, nullable=False)

    __table_args__ = (
        # Covering index for the Task 4 aggregate (appearances + wins per card).
        Index("ix_bridge_deck_cards_card_id", "card_id", postgresql_include=["is_winner"]),
    )


ALL_TABLES = [t.name for t in Base.metadata.sorted_tables]
