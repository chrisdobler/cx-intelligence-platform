"""ORM schema — the canonical data foundation (Phase 2).

Raw source data (immutable once imported) lives in :class:`Conversation` and
:class:`Message`. AI-derived data lives exclusively in
:class:`ConversationAnalysis.analysis_json` (Phase 3) and :class:`Anomaly`
(Phase 4) — business services never mutate imported source rows. Embedding
tables arrive in Phase 5. Schema changes are managed with Alembic
(``migrations/`` at the repo root).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Conversation(Base):
    """A support conversation imported from the source dataset (source data only)."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    external_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    priority: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    issue_type: Mapped[str] = mapped_column(String)
    product: Mapped[str] = mapped_column(String)
    day: Mapped[int] = mapped_column(Integer, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolution_type: Mapped[str | None] = mapped_column(String)
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", order_by="Message.created_at"
    )


class Message(Base):
    """A single message within a conversation (source data only)."""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    external_id: Mapped[str] = mapped_column(String, unique=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ConversationAnalysis(Base):
    """AI-generated understanding of a conversation (Phase 3 writes these).

    ``analysis_json`` holds the canonical Structured Conversation Object as
    JSONB so the AI schema can evolve without database migrations. One analysis
    per conversation — the conversation id is the primary key.
    """

    __tablename__ = "conversation_analyses"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String)
    model_version: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    analysis_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ConversationIssue(Base):
    """Relational projection of one Issue from the Structured Conversation Object.

    Derived data: regenerated 1:1 from ``ConversationAnalysis.analysis_json``
    whenever understanding reruns — never edited manually, never the source of
    truth. Exists so anomaly detection, analytics, and reporting query SQL
    instead of repeatedly parsing JSONB (ADR-006/007).
    """

    __tablename__ = "conversation_issues"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    canonical_name: Mapped[str] = mapped_column(String, index=True)
    customer_description: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)
    customer_impact: Mapped[str] = mapped_column(String)
    product: Mapped[str] = mapped_column(String)
    symptoms: Mapped[list[str]] = mapped_column(JSONB)
    catalog_matched: Mapped[bool] = mapped_column(Boolean, index=True)
    catalog_confidence: Mapped[float] = mapped_column(Float)
    resolution_status: Mapped[str] = mapped_column(String)
    resolution_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IssueCatalogEntry(Base):
    """One category in the platform's issue taxonomy, derived from Day 1 only.

    The catalog is regenerable derived data (ADR-011) — never manually
    maintained. Day 2/3 issues that match no entry stay out of the catalog and
    surface as candidate novel issues (``ConversationIssue.catalog_matched``).
    """

    __tablename__ = "issue_catalog"

    canonical_name: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    first_seen_day: Mapped[int] = mapped_column(Integer)
    example_count: Mapped[int] = mapped_column(Integer)
    representative_examples: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PipelineRun(Base):
    """One execution of a pipeline stage — the run-level audit trail.

    Every stage run is recorded: what ran, when, what triggered it (API or
    CLI), and how it ended. A row stuck in ``running`` is evidence of a
    crashed process. Per-AI-call observability (model, latency, token usage —
    Phase 7) will reference these runs.
    """

    __tablename__ = "pipeline_runs"
    __table_args__ = (Index("ix_pipeline_runs_stage_key_started_at", "stage_key", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    stage_key: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)  # running | succeeded | failed
    trigger: Mapped[str] = mapped_column(String)  # api | cli
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class LLMCallObservation(Base):
    """Per-conversation LLM timing observation for bottleneck analysis."""

    __tablename__ = "llm_call_observations"
    __table_args__ = (
        Index("ix_llm_call_observations_pipeline_run_id", "pipeline_run_id"),
        Index("ix_llm_call_observations_conversation_id", "conversation_id"),
        Index("ix_llm_call_observations_total_seconds", "total_seconds"),
        Index("ix_llm_call_observations_llm_seconds", "llm_seconds"),
        Index("ix_llm_call_observations_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_runs.id"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    day: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    total_seconds: Mapped[float] = mapped_column(Float)
    load_seconds: Mapped[float] = mapped_column(Float)
    prompt_seconds: Mapped[float] = mapped_column(Float)
    llm_seconds: Mapped[float] = mapped_column(Float)
    persist_seconds: Mapped[float] = mapped_column(Float)
    message_count: Mapped[int] = mapped_column(Integer)
    prompt_characters: Mapped[int] = mapped_column(Integer)
    issue_count: Mapped[int] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Anomaly(Base):
    """A detected operational anomaly — the canonical Phase 4 artifact.

    Derived data: every anomaly-detection run regenerates all rows from the
    ``conversation_issues`` projections. ``signals`` lists every detection
    signal that fired (ADR-012) and ``metrics`` carries the numbers behind
    them, so each anomaly explains itself. Slack alerts and reports consume
    these rows; they never analyze operational data independently.
    """

    __tablename__ = "anomalies"
    __table_args__ = (Index("ix_anomalies_day_issue", "day", "issue", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    day: Mapped[int] = mapped_column(Integer, index=True)
    issue: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    delta: Mapped[float] = mapped_column(Float)  # percent_change (0.0 for novel issues)
    description: Mapped[str] = mapped_column(Text)  # human-readable summary
    slack_message: Mapped[str] = mapped_column(Text)
    signals: Mapped[list[str]] = mapped_column(JSONB)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recommended_action: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
