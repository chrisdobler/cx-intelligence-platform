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

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, Uuid
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


class Anomaly(Base):
    """A detected operational anomaly (Phase 4 writes these)."""

    __tablename__ = "anomalies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    day: Mapped[int] = mapped_column(Integer, index=True)
    issue: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    delta: Mapped[float] = mapped_column(Float)
    description: Mapped[str] = mapped_column(Text)
    slack_message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
