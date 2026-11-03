"""Integration fixtures: a throwaway database on the stack's PostgreSQL per session."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text  # noqa: E402

from decksupreme.config import load_settings  # noqa: E402
from decksupreme.db.session import init_db  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    pg = load_settings().postgres
    admin = create_engine(pg.sqlalchemy_url, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL not reachable at {pg.host}:{pg.port} ({exc.__class__.__name__})")

    db_name = f"decksupreme_test_{uuid.uuid4().hex[:8]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    test_url = replace(pg, db=db_name).sqlalchemy_url
    eng = create_engine(test_url, future=True)
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def schema(engine):
    return init_db(engine)


@pytest.fixture
def clean(engine, schema):
    yield engine
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE bridge_deck_cards, fact_matches, dim_players, dim_cards"))
