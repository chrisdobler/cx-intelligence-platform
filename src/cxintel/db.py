"""Database access.

Phase 1 provides connectivity and a health check only — the ORM schema and
migrations arrive in Phase 2. A single engine/session factory is shared across
the process so connection pooling behaves predictably.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Return the shared SQLAlchemy engine."""
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Return the shared session factory."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@dataclass
class DBHealth:
    """Result of a database health probe."""

    connected: bool
    pgvector_installed: bool
    server_version: str | None = None
    error: str | None = None


def check_health() -> DBHealth:
    """Probe database connectivity and confirm the pgvector extension exists."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            server_version = conn.execute(text("show server_version")).scalar_one()
            has_vector = conn.execute(
                text("select exists(select 1 from pg_extension where extname = 'vector')")
            ).scalar_one()
        return DBHealth(
            connected=True,
            pgvector_installed=bool(has_vector),
            server_version=str(server_version),
        )
    except Exception as exc:  # surface any connectivity failure to the caller as data
        return DBHealth(connected=False, pgvector_installed=False, error=str(exc))
