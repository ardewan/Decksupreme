"""Engine and session factory for the warehouse, configured from decksupreme.config."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from decksupreme.config import get_settings
from decksupreme.db.models import Base


@lru_cache(maxsize=8)
def get_engine(url: str | None = None) -> Engine:
    """Return a pooled engine (one per URL). Defaults to the configured warehouse."""
    return create_engine(
        url or get_settings().postgres.sqlalchemy_url,
        future=True,
        pool_pre_ping=True,  # survive Postgres restarts between weekly runs
        pool_size=5,
        max_overflow=5,
    )


def get_sessionmaker(engine: Engine | None = None) -> sessionmaker:
    return sessionmaker(bind=engine or get_engine(), future=True, expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = get_sessionmaker(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine | None = None, *, drop_first: bool = False) -> list[str]:
    """Create all warehouse tables and indexes (idempotent). Returns the table names.

    Intended for local bootstrap and tests; schema changes after the first
    release go through Alembic migrations.
    """
    engine = engine or get_engine()
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return [t.name for t in Base.metadata.sorted_tables]
