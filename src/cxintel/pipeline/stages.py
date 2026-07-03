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
    ) -> str:
        """Execute the stage; returns a one-line human summary."""
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
    # Phase 5 replaces this with a real count once the embedding tables exist.
    return 0


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
    """Phase 3 — LLM extraction of structured conversation objects."""

    key = "understand"
    label = "Conversation Understanding"
    description = (
        "LLM extraction of summary, issues, severity, products, and resolution "
        "for every conversation, persisted as structured JSON."
    )
    outputs = ("conversation analyses",)
    planned_phase = "Phase 3"

    def is_complete(self, session: Session | None) -> bool:
        return bool(_count_or_none(session, _analysis_count))

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


class KnowledgeBaseStage(PipelineStage):
    """Phase 5 — embed resolved conversations for semantic retrieval."""

    key = "knowledge_base"
    label = "Knowledge Base"
    description = (
        "Embed resolved conversation summaries with pgvector so similar "
        "historical cases can be retrieved semantically."
    )
    outputs = ("embeddings",)
    planned_phase = "Phase 5"

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


class AnomalyStage(PipelineStage):
    """Phase 4 — detect emerging operational issues across days."""

    key = "anomaly"
    label = "Anomaly Detection"
    description = (
        "Aggregate issue counts across days to detect new clusters, spikes, and "
        "trends, generating severity-rated Slack alerts."
    )
    outputs = ("anomalies", "Slack alerts")
    planned_phase = "Phase 4"

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
