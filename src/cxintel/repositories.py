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

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by, insert
from sqlalchemy.orm import Session

from .models import (
    Anomaly,
    AnomalyStageObservation,
    Conversation,
    ConversationAnalysis,
    ConversationIssue,
    ConversationUnderstandingFailure,
    EvaluationRun,
    IssueCatalogEntry,
    KnowledgeDocumentRecord,
    LLMCallObservation,
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

    def external_ids_by_ids(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Map conversation ids to their stable external ids."""
        if not ids:
            return {}
        rows = self._session.execute(
            select(Conversation.id, Conversation.external_id).where(Conversation.id.in_(list(ids)))
        )
        return dict(rows.tuples().all())

    def days(self) -> list[int]:
        """Distinct dataset days in ascending order."""
        return list(
            self._session.execute(
                select(Conversation.day).distinct().order_by(Conversation.day)
            ).scalars()
        )

    def day_starts(self) -> dict[int, datetime]:
        """First conversation start per dataset day (timeline day markers)."""
        rows = (
            self._session.execute(
                select(Conversation.day, func.min(Conversation.started_at))
                .group_by(Conversation.day)
                .order_by(Conversation.day)
            )
            .tuples()
            .all()
        )
        return dict(rows)

    def earliest_started_at_for_day(self, day: int) -> datetime | None:
        """Earliest conversation start in a dataset day bucket."""
        return self._session.execute(
            select(func.min(Conversation.started_at)).where(Conversation.day == day)
        ).scalar_one()

    def pending_analysis_ids_for_day(
        self,
        day: int,
        limit: int | None = None,
        *,
        include_terminal_failures: bool = False,
    ) -> list[uuid.UUID]:
        """Conversations on a day still eligible for understanding."""
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
        if not include_terminal_failures:
            stmt = stmt.outerjoin(
                ConversationUnderstandingFailure,
                ConversationUnderstandingFailure.conversation_id == Conversation.id,
            ).where(ConversationUnderstandingFailure.conversation_id.is_(None))
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.execute(stmt).scalars())

    def terminal_failure_ids_for_day(self, day: int, limit: int | None = None) -> list[uuid.UUID]:
        """Terminal-failed conversations that can be retried explicitly."""
        stmt = (
            select(Conversation.id)
            .join(
                ConversationUnderstandingFailure,
                ConversationUnderstandingFailure.conversation_id == Conversation.id,
            )
            .outerjoin(
                ConversationAnalysis,
                ConversationAnalysis.conversation_id == Conversation.id,
            )
            .where(Conversation.day == day)
            .where(ConversationAnalysis.conversation_id.is_(None))
            .order_by(
                ConversationUnderstandingFailure.last_failed_at,
                Conversation.started_at,
                Conversation.id,
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.execute(stmt).scalars())

    def count_for_day(self, day: int) -> int:
        return self._session.execute(
            select(func.count()).select_from(Conversation).where(Conversation.day == day)
        ).scalar_one()

    def analyzed_count_for_day(self, day: int) -> int:
        return self._session.execute(
            select(func.count())
            .select_from(Conversation)
            .join(
                ConversationAnalysis,
                ConversationAnalysis.conversation_id == Conversation.id,
            )
            .where(Conversation.day == day)
        ).scalar_one()

    def terminal_failure_count_for_day(self, day: int) -> int:
        return self._session.execute(
            select(func.count())
            .select_from(Conversation)
            .join(
                ConversationUnderstandingFailure,
                ConversationUnderstandingFailure.conversation_id == Conversation.id,
            )
            .outerjoin(
                ConversationAnalysis,
                ConversationAnalysis.conversation_id == Conversation.id,
            )
            .where(Conversation.day == day)
            .where(ConversationAnalysis.conversation_id.is_(None))
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

    def conversation_ids(self) -> list[uuid.UUID]:
        """Every analyzed conversation id, in a stable order."""
        return list(
            self._session.execute(
                select(ConversationAnalysis.conversation_id).order_by(
                    ConversationAnalysis.conversation_id
                )
            ).scalars()
        )


class ConversationUnderstandingFailureRepository:
    """Persistence for terminal Conversation Understanding failures."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, conversation_id: uuid.UUID) -> ConversationUnderstandingFailure | None:
        return self._session.get(ConversationUnderstandingFailure, conversation_id)

    def count(self) -> int:
        return self._session.execute(
            select(func.count()).select_from(ConversationUnderstandingFailure)
        ).scalar_one()

    def upsert(
        self,
        *,
        conversation_id: uuid.UUID,
        pipeline_run_id: uuid.UUID | None,
        day: int,
        model: str,
        prompt_version: str,
        status: str,
        failure_category: str,
        error: str,
        retry_count: int,
        failed_at: datetime,
    ) -> None:
        existing = self.get(conversation_id)
        if existing is None:
            self._session.add(
                ConversationUnderstandingFailure(
                    conversation_id=conversation_id,
                    pipeline_run_id=pipeline_run_id,
                    day=day,
                    model=model,
                    prompt_version=prompt_version,
                    status=status,
                    failure_category=failure_category,
                    error=error,
                    retry_count=retry_count,
                    first_failed_at=failed_at,
                    last_failed_at=failed_at,
                )
            )
            return
        existing.pipeline_run_id = pipeline_run_id
        existing.day = day
        existing.model = model
        existing.prompt_version = prompt_version
        existing.status = status
        existing.failure_category = failure_category
        existing.error = error
        existing.retry_count = retry_count
        existing.last_failed_at = failed_at

    def clear(self, conversation_id: uuid.UUID) -> None:
        self._session.query(ConversationUnderstandingFailure).filter(
            ConversationUnderstandingFailure.conversation_id == conversation_id
        ).delete()


class IssueAggregate:
    """Per-canonical-name Day-1 aggregation used to build the issue catalog."""

    def __init__(self, canonical_name: str, example_count: int, examples: list[str]) -> None:
        self.canonical_name = canonical_name
        self.example_count = example_count
        self.examples = examples


class IssueDayStats:
    """Per-canonical-name operational statistics for one day (anomaly inputs)."""

    def __init__(
        self,
        canonical_name: str,
        count: int,
        high_severity_count: int,
        resolved_count: int,
        unmatched_count: int,
    ) -> None:
        self.canonical_name = canonical_name
        self.count = count
        self.high_severity_count = high_severity_count
        self.resolved_count = resolved_count
        self.unmatched_count = unmatched_count


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

    def issue_timeline(
        self, issue: str, *, bucket_seconds: int = 3600
    ) -> list[tuple[datetime, int]]:
        """Occurrence counts for one issue, bucketed by conversation start time.

        Buckets are epoch-aligned floors of ``Conversation.started_at`` so the
        bucket size stays configurable (V1 renders hourly). Presentation data
        only — anomaly detection never reads this.
        """
        bucket = func.to_timestamp(
            func.floor(func.extract("epoch", Conversation.started_at) / bucket_seconds)
            * bucket_seconds
        )
        rows = (
            self._session.execute(
                select(bucket, func.count())
                .select_from(ConversationIssue)
                .join(Conversation, Conversation.id == ConversationIssue.conversation_id)
                .where(ConversationIssue.canonical_name == issue)
                .group_by(bucket)
                .order_by(bucket)
            )
            .tuples()
            .all()
        )
        return [(start, count) for start, count in rows]

    def day_issue_stats(self, day: int) -> list[IssueDayStats]:
        """Per-canonical-name operational statistics for one day (anomaly inputs).

        One grouped SQL query: count, high/critical-severity count, resolved
        count, and catalog-unmatched count per issue category.
        """
        high = case((ConversationIssue.severity.in_(["high", "critical"]), 1), else_=0)
        resolved = case((ConversationIssue.resolution_status == "resolved", 1), else_=0)
        unmatched = case((ConversationIssue.catalog_matched.is_(False), 1), else_=0)
        rows = (
            self._session.execute(
                select(
                    ConversationIssue.canonical_name,
                    func.count(),
                    func.sum(high),
                    func.sum(resolved),
                    func.sum(unmatched),
                )
                .join(Conversation, Conversation.id == ConversationIssue.conversation_id)
                .where(Conversation.day == day)
                .group_by(ConversationIssue.canonical_name)
                .order_by(ConversationIssue.canonical_name)
            )
            .tuples()
            .all()
        )
        return [
            IssueDayStats(name, count, int(high_n), int(resolved_n), int(unmatched_n))
            for name, count, high_n, resolved_n, unmatched_n in rows
        ]

    def aggregate_for_day(self, day: int) -> list[IssueAggregate]:
        """Group a day's issues by canonical name with example descriptions."""
        rows = (
            self._session.execute(
                select(
                    ConversationIssue.canonical_name,
                    func.count(),
                    func.array_agg(
                        aggregate_order_by(
                            ConversationIssue.customer_description,
                            Conversation.started_at,
                            Conversation.id,
                            ConversationIssue.id,
                        )
                    ),
                )
                .join(Conversation, Conversation.id == ConversationIssue.conversation_id)
                .where(Conversation.day == day)
                .group_by(ConversationIssue.canonical_name)
            )
            .tuples()
            .all()
        )
        return [IssueAggregate(name, count, list(examples)) for name, count, examples in rows]


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


LLM_OBSERVATION_SORT_FIELDS = {
    "total_seconds": LLMCallObservation.total_seconds,
    "llm_seconds": LLMCallObservation.llm_seconds,
    "load_seconds": LLMCallObservation.load_seconds,
    "prompt_seconds": LLMCallObservation.prompt_seconds,
    "persist_seconds": LLMCallObservation.persist_seconds,
    "retry_count": LLMCallObservation.retry_count,
    "started_at": LLMCallObservation.started_at,
}


class LLMCallObservationRepository:
    """Persistence and slow-call queries for LLM timing observations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, observation: LLMCallObservation) -> None:
        self._session.add(observation)

    def count(self) -> int:
        return self._session.execute(
            select(func.count()).select_from(LLMCallObservation)
        ).scalar_one()

    def slowest(
        self,
        *,
        limit: int = 20,
        sort: str = "total_seconds",
        pipeline_run_id: uuid.UUID | None = None,
    ) -> list[LLMCallObservation]:
        """Return recent/slow observations sorted by an allowed diagnostic field."""
        sort_column = LLM_OBSERVATION_SORT_FIELDS.get(sort)
        if sort_column is None:
            raise ValueError(f"Unsupported LLM observation sort '{sort}'.")
        stmt = select(LLMCallObservation)
        if pipeline_run_id is not None:
            stmt = stmt.where(LLMCallObservation.pipeline_run_id == pipeline_run_id)
        return list(
            self._session.execute(
                stmt.order_by(sort_column.desc(), LLMCallObservation.started_at.desc()).limit(limit)
            ).scalars()
        )


ANOMALY_OBSERVATION_SORT_FIELDS = {
    "total_seconds": AnomalyStageObservation.total_seconds,
    "started_at": AnomalyStageObservation.started_at,
    "anomalies_detected": AnomalyStageObservation.anomalies_detected,
    "alert_count": AnomalyStageObservation.alert_count,
    "fallback_count": AnomalyStageObservation.fallback_count,
    "delivered_count": AnomalyStageObservation.delivered_count,
}


class AnomalyStageObservationRepository:
    """Persistence and slow-step queries for anomaly detection observations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, observation: AnomalyStageObservation) -> None:
        self._session.add(observation)

    def count(self) -> int:
        return self._session.execute(
            select(func.count()).select_from(AnomalyStageObservation)
        ).scalar_one()

    def slowest(
        self,
        *,
        limit: int = 20,
        sort: str = "total_seconds",
        pipeline_run_id: uuid.UUID | None = None,
    ) -> list[AnomalyStageObservation]:
        """Return recent/slow observations sorted by an allowed diagnostic field."""
        sort_column = ANOMALY_OBSERVATION_SORT_FIELDS.get(sort)
        if sort_column is None:
            raise ValueError(f"Unsupported anomaly observation sort '{sort}'.")
        stmt = select(AnomalyStageObservation)
        if pipeline_run_id is not None:
            stmt = stmt.where(AnomalyStageObservation.pipeline_run_id == pipeline_run_id)
        return list(
            self._session.execute(
                stmt.order_by(sort_column.desc(), AnomalyStageObservation.started_at.desc()).limit(
                    limit
                )
            ).scalars()
        )


class EvaluationRunRepository:
    """Persistence for :class:`EvaluationRun` rows — the Phase 7 evaluation history."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: EvaluationRun) -> None:
        self._session.add(run)

    def count(self) -> int:
        return self._session.execute(select(func.count()).select_from(EvaluationRun)).scalar_one()

    def latest(self) -> EvaluationRun | None:
        """The most recent evaluation run, if any."""
        return self._session.execute(
            select(EvaluationRun).order_by(EvaluationRun.started_at.desc()).limit(1)
        ).scalar_one_or_none()

    def recent(self, limit: int = 20) -> list[EvaluationRun]:
        """The most recent evaluation runs, newest first."""
        return list(
            self._session.execute(
                select(EvaluationRun).order_by(EvaluationRun.started_at.desc()).limit(limit)
            ).scalars()
        )


class KnowledgeDocumentRepository:
    """Persistence + vector search for :class:`KnowledgeDocumentRecord` rows."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def count(self) -> int:
        return self._session.execute(
            select(func.count()).select_from(KnowledgeDocumentRecord)
        ).scalar_one()

    def all(self) -> list[KnowledgeDocumentRecord]:
        return list(
            self._session.execute(
                select(KnowledgeDocumentRecord).order_by(
                    KnowledgeDocumentRecord.issue, KnowledgeDocumentRecord.id
                )
            ).scalars()
        )

    def source_external_ids(self) -> set[str]:
        """External ids of every conversation with at least one knowledge document."""
        return set(
            self._session.execute(
                select(Conversation.external_id)
                .join(
                    KnowledgeDocumentRecord,
                    KnowledgeDocumentRecord.conversation_id == Conversation.id,
                )
                .distinct()
            ).scalars()
        )

    def for_conversation(self, conversation_id: uuid.UUID) -> list[KnowledgeDocumentRecord]:
        return list(
            self._session.execute(
                select(KnowledgeDocumentRecord)
                .where(KnowledgeDocumentRecord.conversation_id == conversation_id)
                .order_by(KnowledgeDocumentRecord.issue, KnowledgeDocumentRecord.id)
            ).scalars()
        )

    def replace_for_conversation(
        self, conversation_id: uuid.UUID, rows: Sequence[KnowledgeDocumentRecord]
    ) -> None:
        """Regenerate one conversation's documents (derived data — delete + insert)."""
        self._session.query(KnowledgeDocumentRecord).filter(
            KnowledgeDocumentRecord.conversation_id == conversation_id
        ).delete()
        self._session.add_all(rows)

    def search(
        self,
        embedding: Sequence[float],
        *,
        product: str | None = None,
        limit: int = 5,
    ) -> list[tuple[KnowledgeDocumentRecord, float]]:
        """Nearest documents by cosine distance, optionally metadata-filtered.

        Every stored document is resolved by construction, so 'resolved only'
        needs no filter here. The caller owns filter-relaxation policy.
        """
        distance = KnowledgeDocumentRecord.embedding.cosine_distance(list(embedding))
        stmt = select(KnowledgeDocumentRecord, distance)
        if product is not None:
            stmt = stmt.where(KnowledgeDocumentRecord.product == product)
        stmt = stmt.order_by(distance, KnowledgeDocumentRecord.id).limit(limit)
        return [(row, float(dist)) for row, dist in self._session.execute(stmt).tuples()]


class AnomalyRepository:
    """Persistence for :class:`Anomaly` rows — the canonical Phase 4 artifact."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, anomaly: Anomaly) -> None:
        self._session.add(anomaly)

    def replace_all(self, anomalies: Sequence[Anomaly]) -> None:
        """Regenerate the whole anomaly set (derived data — reruns never duplicate)."""
        self._session.query(Anomaly).delete()
        self._session.add_all(anomalies)

    def for_days(self, days: Sequence[int]) -> list[Anomaly]:
        """Anomalies for the given days, ordered by day then issue."""
        return list(
            self._session.execute(
                select(Anomaly)
                .where(Anomaly.day.in_(list(days)))
                .order_by(Anomaly.day, Anomaly.issue)
            ).scalars()
        )

    def all(self) -> list[Anomaly]:
        return list(
            self._session.execute(select(Anomaly).order_by(Anomaly.day, Anomaly.issue)).scalars()
        )

    def count(self) -> int:
        return self._session.execute(select(func.count()).select_from(Anomaly)).scalar_one()
