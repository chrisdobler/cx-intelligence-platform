"""Shared test fixtures.

Database-backed tests run against a throwaway ``cx_test`` database on the
local dev Postgres (``make up``), migrated to head via Alembic — so the
migration itself is exercised. When Postgres is unreachable those tests are
skipped and the rest of the suite still passes.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADMIN_URL = "postgresql+psycopg://cx:cx@localhost:5432/cx"
_TEST_DB = f"cx_test_{os.getpid()}"
_TEST_URL = f"postgresql+psycopg://cx:cx@localhost:5432/{_TEST_DB}"


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    """Engine bound to a freshly created, fully migrated ``cx_test`` database."""
    admin_engine = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'drop database if exists "{_TEST_DB}" (force)'))
            conn.execute(text(f'create database "{_TEST_DB}"'))
    except Exception as exc:
        pytest.skip(f"postgres unavailable — run 'make up' ({exc})")

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _TEST_URL)
    command.upgrade(cfg, "head")

    engine = create_engine(_TEST_URL)
    yield engine
    engine.dispose()
    with admin_engine.connect() as conn:
        conn.execute(text(f'drop database if exists "{_TEST_DB}" (force)'))
    admin_engine.dispose()


@pytest.fixture
def settings_on_test_db(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point application settings (and cached engines) at the test database.

    For tests that exercise code resolving the DB through ``get_settings()`` —
    the CLI and the status API — rather than taking an explicit session.
    """
    from cxintel.config import get_settings
    from cxintel.db import get_engine, get_session_factory

    monkeypatch.setenv("DATABASE_URL", _TEST_URL)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield _TEST_URL
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    """A session on the test database, with tables truncated after each test."""
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    session = factory()
    yield session
    try:
        session.rollback()
    except OperationalError:
        pass
    finally:
        session.close()
    try:
        with migrated_engine.connect() as conn:
            for table in (
                "conversation_understanding_failures",
                "llm_call_observations",
                "knowledge_documents",
                "conversation_issues",
                "issue_catalog",
                "anomalies",
                "conversation_analyses",
                "pipeline_runs",
                "messages",
                "conversations",
            ):
                conn.execute(text(f"delete from {table}"))
            conn.commit()
    except OperationalError:
        pass
