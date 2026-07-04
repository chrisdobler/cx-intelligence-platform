"""Pipeline stage definitions — the common interface every stage exposes.

Each processing stage is an independently executable job that reports its
completion state and prerequisites, and (for batch stages) can be run through
the orchestrator. Stage classes here stay thin: the heavy business logic lives
in each phase's own package (:mod:`cxintel.ingestion` today; understanding,
anomaly detection, knowledge base, and the resolution assistant as their
phases land). A stage that is not yet implemented says so via ``implemented``
/ ``planned_phase`` rather than pretending to run.

All database-touching checks degrade gracefully (unreachable database → stage
not complete, prerequisite unmet with a clear reason) so the status surface
keeps working with the infrastructure down.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from .progress import ProgressCallback, ProgressReporter


class StageKind(StrEnum):
    """Batch stages run to completion; interactive stages are opened, not run."""

    BATCH = "batch"
    INTERACTIVE = "interactive"


class Prerequisite(BaseModel):
    """One condition a stage needs before it can run, with a human explanation."""

    label: str
    met: bool
    detail: str | None = None


class RunOption(BaseModel):
    """One explicit way to run a stage (e.g. sample vs full dataset).

    The first option is the stage's default — used by Run Remaining and by
    runs that specify no option. Stages without options run one way only.
    """

    value: str
    label: str


class StageNotRunnableError(Exception):
    """Raised when run() is invoked on an unimplemented or interactive stage."""


class PipelineStage(ABC):
    """Common interface for one pipeline stage."""

    key: str
    label: str
    description: str
    outputs: tuple[str, ...]
    kind: StageKind = StageKind.BATCH
    implemented: bool = False
    planned_phase: str | None = None
    open_url: str | None = None
    run_options: tuple[RunOption, ...] = ()

    @abstractmethod
    def is_complete(self, session: Session | None) -> bool:
        """Whether this stage's output already exists (derived from the data)."""

    @abstractmethod
    def prerequisites(self, session: Session | None) -> list[Prerequisite]:
        """The conditions required before this stage can run."""

    def run(
        self,
        session_factory: sessionmaker[Session],
        progress: ProgressCallback,
        option: str | None = None,
        run_id: uuid.UUID | None = None,
    ) -> str:
        """Execute the stage; returns a one-line human summary.

        ``option`` selects one of ``run_options`` for stages that expose
        explicit run modes; stages without options ignore it (the orchestrator
        validates it before calling).
        """
        if self.kind is StageKind.INTERACTIVE:
            raise StageNotRunnableError(f"'{self.label}' is interactive — open it instead.")
        raise StageNotRunnableError(
            f"'{self.label}' is not yet implemented"
            + (f" (planned for {self.planned_phase})." if self.planned_phase else ".")
        )


def _database_unreachable() -> Prerequisite:
    return Prerequisite(
        label="Database reachable",
        met=False,
        detail="PostgreSQL is unreachable — run 'make up'.",
    )


def _ai_prerequisite() -> Prerequisite:
    from ..config import get_settings

    configured = get_settings().ai_configured
    return Prerequisite(
        label="Google AI configured",
        met=configured,
        detail=None if configured else "Add your GOOGLE_API_KEY in the AI Capabilities card.",
    )


def _count_or_none(session: Session | None, count: Callable[[Session], int]) -> int | None:
    """A repository count, or None when the database is unavailable."""
    if session is None:
        return None
    try:
        return count(session)
    except Exception:
        return None


def _conversation_count(session: Session) -> int:
    from ..repositories import ConversationRepository

    return ConversationRepository(session).count()


def _analysis_count(session: Session) -> int:
    from ..repositories import ConversationAnalysisRepository

    return ConversationAnalysisRepository(session).count()


def _anomaly_count(session: Session) -> int:
    from ..repositories import AnomalyRepository

    return AnomalyRepository(session).count()


def _embedding_count(session: Session) -> int:
    from ..repositories import KnowledgeDocumentRepository

    return KnowledgeDocumentRepository(session).count()


class IngestStage(PipelineStage):
    """Phase 2 — import the raw ticket dataset into PostgreSQL."""

    key = "ingest"
    label = "Data Ingestion"
    description = (
        "Validate and import the raw ticket dataset into PostgreSQL. "
        "Idempotent — rerunning skips rows that already exist."
    )
    outputs = ("conversations", "messages")
    implemented = True

    def is_complete(self, session: Session | None) -> bool:
        return bool(_count_or_none(session, _conversation_count))

    def prerequisites(self, session: Session | None) -> list[Prerequisite]:
        from ..config import get_settings

        path = Path(get_settings().raw_data_path)
        dataset = Prerequisite(
            label="Raw dataset present",
            met=path.exists(),
            detail=None if path.exists() else f"Place the dataset at {path}.",
        )
        if session is None:
            return [dataset, _database_unreachable()]
        return [dataset, Prerequisite(label="Database reachable", met=True)]

    def run(
        self,
        session_factory: sessionmaker[Session],
        progress: ProgressCallback,
        option: str | None = None,
        run_id: uuid.UUID | None = None,
    ) -> str:
        from alembic import command
        from alembic.config import Config

        from ..config import get_settings
        from ..ingestion.service import IngestionService

        reporter = ProgressReporter(
            stage_key=self.key,
            stage_label=self.label,
            progress=progress,
            message="Applying database migrations…",
        )
        command.upgrade(Config("alembic.ini"), "head")

        reporter.report(message="Validating and importing the dataset…")
        with session_factory() as session:
            result = IngestionService(session).ingest(
                Path(get_settings().raw_data_path), progress=reporter
            )

        conv_skipped = result.conversations_seen - result.conversations_inserted
        msg_skipped = result.messages_seen - result.messages_inserted
        return (
            f"Ingested {result.conversations_seen} conversations ({conv_skipped} skipped), "
            f"{result.messages_seen} messages ({msg_skipped} skipped)."
        )


class UnderstandStage(PipelineStage):
    """Phase 3 — LLM extraction of the canonical Structured Conversation Object."""

    key = "understand"
    label = "Conversation Understanding"
    description = (
        "Gemini extracts the canonical Structured Conversation Object (summary, "
        "issues, resolution) for every conversation, projects issues relationally, "
        "and derives the Day-1 issue catalog. Resumable — reruns skip analyzed "
        "conversations."
    )
    outputs = ("conversation analyses", "conversation issues", "issue catalog")
    implemented = True
    run_options = (
        RunOption(value="sample", label="Run Sample (100)"),
        RunOption(value="full", label="Run Full Dataset"),
        RunOption(value="retry_failures", label="Retry Recorded Failures"),
    )

    def is_complete(self, session: Session | None) -> bool:
        analyses = _count_or_none(session, _analysis_count)
        conversations = _count_or_none(session, _conversation_count)
        if not analyses or not conversations:
            return False
        return analyses >= conversations

    def prerequisites(self, session: Session | None) -> list[Prerequisite]:
        ingested = bool(_count_or_none(session, _conversation_count))
        return [
            Prerequisite(
                label="Dataset imported",
                met=ingested,
                detail=None if ingested else "Run Data Ingestion first.",
            ),
            _ai_prerequisite(),
        ]

    def run(
        self,
        session_factory: sessionmaker[Session],
        progress: ProgressCallback,
        option: str | None = None,
        run_id: uuid.UUID | None = None,
    ) -> str:
        from alembic import command
        from alembic.config import Config

        from ..config import get_settings
        from ..llm import get_llm_provider
        from ..understanding.service import UnderstandingService

        settings = get_settings()
        retry_failures = option == "retry_failures"
        limit = settings.understand_sample_size if option in (None, "sample") else None
        if settings.understand_limit is not None:
            limit = settings.understand_limit  # explicit env override wins

        reporter = ProgressReporter(
            stage_key=self.key,
            stage_label=self.label,
            progress=progress,
            message="Applying database migrations…",
        )
        command.upgrade(Config("alembic.ini"), "head")

        scope = (
            "recorded failures"
            if retry_failures
            else "full dataset"
            if limit is None
            else f"sample of {limit}"
        )
        reporter.report(message=f"Running conversation understanding ({scope})…")
        service = UnderstandingService(session_factory, get_llm_provider(), pipeline_run_id=run_id)
        return service.run(
            limit=limit, progress=reporter, retry_failures=retry_failures
        ).summary()


class KnowledgeBaseStage(PipelineStage):
    """Phase 5 — deterministic knowledge synthesis + embeddings for retrieval."""

    key = "knowledge_base"
    label = "Knowledge Base"
    description = (
        "Deterministically distill every resolved issue into a KnowledgeDocument, "
        "render its knowledge_text, and embed it with pgvector for semantic "
        "retrieval. No LLM — only the embedding model. Reruns re-embed only new "
        "or changed documents."
    )
    outputs = ("knowledge documents", "embeddings")
    implemented = True

    def is_complete(self, session: Session | None) -> bool:
        return bool(_count_or_none(session, _embedding_count))

    def prerequisites(self, session: Session | None) -> list[Prerequisite]:
        understood = bool(_count_or_none(session, _analysis_count))
        return [
            Prerequisite(
                label="Conversations understood",
                met=understood,
                detail=None if understood else "Run Conversation Understanding first.",
            ),
            _ai_prerequisite(),
        ]

    def run(
        self,
        session_factory: sessionmaker[Session],
        progress: ProgressCallback,
        option: str | None = None,
        run_id: uuid.UUID | None = None,
    ) -> str:
        from alembic import command
        from alembic.config import Config

        from ..knowledge_base.service import KnowledgeBaseService
        from ..llm import get_embedding_provider

        reporter = ProgressReporter(
            stage_key=self.key,
            stage_label=self.label,
            progress=progress,
            message="Applying database migrations…",
        )
        command.upgrade(Config("alembic.ini"), "head")

        reporter.report(message="Building the knowledge base…")
        service = KnowledgeBaseService(
            session_factory, get_embedding_provider(), pipeline_run_id=run_id
        )
        return service.run(progress=reporter).summary()


class AnomalyStage(PipelineStage):
    """Phase 4 — deterministic multi-signal anomaly detection over projections."""

    key = "anomaly"
    label = "Anomaly Detection"
    description = (
        "Deterministic rules compare each day's issue statistics against the "
        "Day-1 baseline (volume spikes, novel issues, severity drift, resolution "
        "drift) and produce explainable anomalies, Slack alerts, and a report."
    )
    outputs = ("anomalies", "Slack alerts", "anomaly report")
    implemented = True

    def is_complete(self, session: Session | None) -> bool:
        return bool(_count_or_none(session, _anomaly_count))

    def prerequisites(self, session: Session | None) -> list[Prerequisite]:
        understood = bool(_count_or_none(session, _analysis_count))
        return [
            Prerequisite(
                label="Conversations understood",
                met=understood,
                detail=None if understood else "Run Conversation Understanding first.",
            ),
            _ai_prerequisite(),
        ]

    def run(
        self,
        session_factory: sessionmaker[Session],
        progress: ProgressCallback,
        option: str | None = None,
        run_id: uuid.UUID | None = None,
    ) -> str:
        from alembic import command
        from alembic.config import Config

        from ..anomaly.service import AnomalyService
        from ..llm import get_llm_provider

        reporter = ProgressReporter(
            stage_key=self.key,
            stage_label=self.label,
            progress=progress,
            message="Applying database migrations…",
        )
        command.upgrade(Config("alembic.ini"), "head")

        reporter.report(message="Running anomaly detection…")
        service = AnomalyService(session_factory, get_llm_provider(), pipeline_run_id=run_id)
        return service.run(progress=reporter).summary()


class ResolutionAssistantStage(PipelineStage):
    """Phase 6 — interactive RAG assistant for support agents."""

    key = "resolution_assistant"
    label = "Resolution Assistant"
    description = (
        "Interactive assistant that retrieves similar resolved conversations and "
        "generates a grounded resolution path for a new ticket."
    )
    outputs = ("resolution suggestions",)
    kind = StageKind.INTERACTIVE
    planned_phase = "Phase 6"

    def is_complete(self, session: Session | None) -> bool:
        # An interactive stage is "complete" when it is ready to use.
        return self.implemented and all(p.met for p in self.prerequisites(session))

    def prerequisites(self, session: Session | None) -> list[Prerequisite]:
        kb_ready = bool(_count_or_none(session, _embedding_count))
        return [
            Prerequisite(
                label="Knowledge base built",
                met=kb_ready,
                detail=None if kb_ready else "Run Knowledge Base generation first.",
            ),
            _ai_prerequisite(),
        ]
