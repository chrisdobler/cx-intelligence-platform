"""Repository layer — isolates persistence from business logic.

Services interact with these classes rather than with SQLAlchemy sessions
directly. Bulk inserts use PostgreSQL ``ON CONFLICT DO NOTHING`` so ingestion
is idempotent: rerunning skips rows that already exist and reports how many
were actually inserted. Transaction boundaries (commit/rollback) belong to the
caller.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import Anomaly, Conversation, ConversationAnalysis, Message, PipelineRun


class ConversationRepository:
    """Persistence for :class:`Conversation` rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def bulk_insert_ignore_conflicts(self, rows: Sequence[dict[str, Any]]) -> int:
        """Insert rows, skipping any that already exist. Returns rows inserted."""
        if not rows:
            return 0
        # RETURNING gives an exact inserted count (conflicting rows return
        # nothing) — cursor rowcount is unreliable for compiled inserts here.
        # A single multi-VALUES statement per call; callers chunk large sets.
        stmt = (
            insert(Conversation)
            .values(list(rows))
            .on_conflict_do_nothing()
            .returning(Conversation.id)
        )
        return len(self._session.connection().execute(stmt).all())

    def count(self) -> int:
        return self._session.execute(select(func.count()).select_from(Conversation)).scalar_one()

    def count_by_status(self) -> dict[str, int]:
        rows = (
            self._session.execute(
                select(Conversation.status, func.count()).group_by(Conversation.status)
            )
            .tuples()
            .all()
        )
        return dict(rows)

    def date_range(self) -> tuple[datetime, datetime] | None:
        """The activity span of the dataset (min started_at, max ended_at)."""
        earliest, latest = self._session.execute(
            select(func.min(Conversation.started_at), func.max(Conversation.ended_at))
        ).one()
        if earliest is None or latest is None:
            return None
        return earliest, latest

    def get_by_external_id(self, external_id: str) -> Conversation | None:
        return self._session.execute(
            select(Conversation).where(Conversation.external_id == external_id)
        ).scalar_one_or_none()


class MessageRepository:
    """Persistence for :class:`Message` rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def bulk_insert_ignore_conflicts(self, rows: Sequence[dict[str, Any]]) -> int:
        """Insert rows, skipping any that already exist. Returns rows inserted."""
        if not rows:
            return 0
        stmt = insert(Message).values(list(rows)).on_conflict_do_nothing().returning(Message.id)
        return len(self._session.connection().execute(stmt).all())

    def count(self) -> int:
        return self._session.execute(select(func.count()).select_from(Message)).scalar_one()


class ConversationAnalysisRepository:
    """Persistence for :class:`ConversationAnalysis` rows (Phase 3 extends this)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, analysis: ConversationAnalysis) -> None:
        self._session.add(analysis)

    def count(self) -> int:
        return self._session.execute(
            select(func.count()).select_from(ConversationAnalysis)
        ).scalar_one()

    def get(self, conversation_id: uuid.UUID) -> ConversationAnalysis | None:
        return self._session.get(ConversationAnalysis, conversation_id)


class PipelineRunRepository:
    """Persistence for :class:`PipelineRun` rows — the pipeline audit trail."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: PipelineRun) -> None:
        self._session.add(run)

    def get(self, run_id: uuid.UUID) -> PipelineRun | None:
        return self._session.get(PipelineRun, run_id)

    def latest_finished_per_stage(self) -> dict[str, PipelineRun]:
        """The most recent finished run for each stage (running rows excluded)."""
        rows = self._session.execute(
            select(PipelineRun)
            .distinct(PipelineRun.stage_key)
            .where(PipelineRun.finished_at.is_not(None))
            .order_by(PipelineRun.stage_key, PipelineRun.started_at.desc())
        ).scalars()
        return {run.stage_key: run for run in rows}

    def recent(self, limit: int = 20) -> list[PipelineRun]:
        """The most recent runs, newest first (running rows included)."""
        return list(
            self._session.execute(
                select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit)
            ).scalars()
        )


class AnomalyRepository:
    """Persistence for :class:`Anomaly` rows (Phase 4 extends this)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, anomaly: Anomaly) -> None:
        self._session.add(anomaly)

    def count(self) -> int:
        return self._session.execute(select(func.count()).select_from(Anomaly)).scalar_one()
