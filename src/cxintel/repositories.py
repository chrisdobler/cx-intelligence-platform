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

from .models import (
    Anomaly,
    Conversation,
    ConversationAnalysis,
    ConversationIssue,
    IssueCatalogEntry,
    Message,
    PipelineRun,
)


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

    def days(self) -> list[int]:
        """Distinct dataset days in ascending order."""
        return list(
            self._session.execute(
                select(Conversation.day).distinct().order_by(Conversation.day)
            ).scalars()
        )

    def pending_analysis_ids_for_day(self, day: int, limit: int | None = None) -> list[uuid.UUID]:
        """Conversations on a day that have no analysis yet (resumable runs)."""
        stmt = (
            select(Conversation.id)
            .outerjoin(
                ConversationAnalysis,
                ConversationAnalysis.conversation_id == Conversation.id,
            )
            .where(Conversation.day == day)
            .where(ConversationAnalysis.conversation_id.is_(None))
            .order_by(Conversation.started_at, Conversation.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.execute(stmt).scalars())

    def count_for_day(self, day: int) -> int:
        return self._session.execute(
            select(func.count()).select_from(Conversation).where(Conversation.day == day)
        ).scalar_one()


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

    def upsert(self, analysis: ConversationAnalysis) -> None:
        """Insert or replace the analysis for a conversation (rerun = regenerate)."""
        self._session.merge(analysis)

    def count(self) -> int:
        return self._session.execute(
            select(func.count()).select_from(ConversationAnalysis)
        ).scalar_one()

    def get(self, conversation_id: uuid.UUID) -> ConversationAnalysis | None:
        return self._session.get(ConversationAnalysis, conversation_id)


class IssueAggregate:
    """Per-canonical-name Day-1 aggregation used to build the issue catalog."""

    def __init__(self, canonical_name: str, example_count: int, examples: list[str]) -> None:
        self.canonical_name = canonical_name
        self.example_count = example_count
        self.examples = examples


class ConversationIssueRepository:
    """Persistence for :class:`ConversationIssue` projections (derived data)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def replace_for_conversation(
        self, conversation_id: uuid.UUID, issues: Sequence[ConversationIssue]
    ) -> None:
        """Regenerate the projection for one conversation (delete + insert)."""
        self._session.query(ConversationIssue).filter(
            ConversationIssue.conversation_id == conversation_id
        ).delete()
        self._session.add_all(issues)

    def count(self) -> int:
        return self._session.execute(
            select(func.count()).select_from(ConversationIssue)
        ).scalar_one()

    def unmatched_count(self) -> int:
        """Candidate novel issues — extracted but absent from the catalog."""
        return self._session.execute(
            select(func.count())
            .select_from(ConversationIssue)
            .where(ConversationIssue.catalog_matched.is_(False))
        ).scalar_one()

    def canonical_names_for_day(self, day: int) -> list[str]:
        """Distinct canonical names seen on one day (in-flight Day-1 normalization)."""
        rows = self._session.execute(
            select(ConversationIssue.canonical_name)
            .join(Conversation, Conversation.id == ConversationIssue.conversation_id)
            .where(Conversation.day == day)
            .distinct()
            .order_by(ConversationIssue.canonical_name)
        ).scalars()
        return list(rows)

    def aggregate_for_day(self, day: int) -> list[IssueAggregate]:
        """Group a day's issues by canonical name with example descriptions."""
        rows = (
            self._session.execute(
                select(
                    ConversationIssue.canonical_name,
                    func.count(),
                    func.array_agg(ConversationIssue.customer_description),
                )
                .join(Conversation, Conversation.id == ConversationIssue.conversation_id)
                .where(Conversation.day == day)
                .group_by(ConversationIssue.canonical_name)
            )
            .tuples()
            .all()
        )
        return [
            IssueAggregate(name, count, list(examples)) for name, count, examples in rows
        ]


class IssueCatalogRepository:
    """Persistence for the derived :class:`IssueCatalogEntry` taxonomy."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def all(self) -> list[IssueCatalogEntry]:
        return list(
            self._session.execute(
                select(IssueCatalogEntry).order_by(IssueCatalogEntry.canonical_name)
            ).scalars()
        )

    def count(self) -> int:
        return self._session.execute(
            select(func.count()).select_from(IssueCatalogEntry)
        ).scalar_one()

    def replace_all(self, entries: Sequence[IssueCatalogEntry]) -> None:
        """Regenerate the whole catalog (it is derived data)."""
        self._session.query(IssueCatalogEntry).delete()
        self._session.add_all(entries)


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
