"""Ingestion service — orchestrates JSON → repositories → PostgreSQL.

Row ids are deterministic (UUIDv5 over the source identifiers), so combined
with the repositories' ``ON CONFLICT DO NOTHING`` inserts the pipeline is
idempotent: rerunning against the same dataset inserts nothing and reports the
skipped counts. Business logic stays here; persistence stays in the
repositories; imported source data is never mutated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..repositories import ConversationRepository, MessageRepository
from .loader import RawConversation, load_raw_conversations

_CONVERSATION_NS = uuid.uuid5(uuid.NAMESPACE_URL, "cxintel/conversation")
_MESSAGE_NS = uuid.uuid5(uuid.NAMESPACE_URL, "cxintel/message")

_CHUNK_SIZE = 1000


def conversation_row(raw: RawConversation) -> dict[str, Any]:
    """Map a raw record onto a ``conversations`` row.

    ``started_at``/``ended_at`` are the conversation's actual activity span
    (first/last message); ``created_at``/``updated_at`` come from source
    metadata. Optional source flags are preserved in ``source_metadata``.
    """
    meta = raw.metadata
    message_times = [m.created_at for m in raw.messages]
    return {
        "id": uuid.uuid5(_CONVERSATION_NS, raw.conversation_id),
        "external_id": raw.conversation_id,
        "customer_id": raw.customer_id,
        "status": meta.status,
        "priority": meta.priority,
        "category": meta.category,
        "issue_type": meta.issue_type,
        "product": meta.product,
        "day": meta.day,
        "started_at": min(message_times),
        "ended_at": max(message_times),
        "created_at": meta.created_at,
        "updated_at": meta.updated_at,
        "resolution_type": raw.resolution.resolution_type if raw.resolution else None,
        "resolution_notes": raw.resolution.resolution_notes if raw.resolution else None,
        "resolved_at": raw.resolution.resolved_at if raw.resolution else None,
        "source_metadata": {
            "has_curveball": meta.has_curveball,
            "spans_multiple_days": meta.spans_multiple_days,
            "is_long_conversation": meta.is_long_conversation,
            "is_multi_issue": meta.is_multi_issue,
            "secondary_issues": meta.secondary_issues,
        },
    }


def message_rows(raw: RawConversation, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    """Map a raw record's messages onto ``messages`` rows (source ``text`` → ``body``)."""
    return [
        {
            "id": uuid.uuid5(_MESSAGE_NS, message.message_id),
            "external_id": message.message_id,
            "conversation_id": conversation_id,
            "role": message.role,
            "body": message.text,
            "created_at": message.created_at,
        }
        for message in raw.messages
    ]


@dataclass
class IngestionResult:
    """Outcome of one ingestion run — seen vs actually inserted."""

    conversations_seen: int
    conversations_inserted: int
    messages_seen: int
    messages_inserted: int


class IngestionService:
    """Imports the raw ticket dataset into PostgreSQL, idempotently."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)

    def ingest(self, path: Path) -> IngestionResult:
        """Load, validate, and persist the dataset at ``path`` in one transaction."""
        records = load_raw_conversations(path)

        conversation_batch: list[dict[str, Any]] = []
        message_batch: list[dict[str, Any]] = []
        conversations_inserted = 0
        messages_inserted = 0
        messages_seen = 0

        def flush() -> None:
            nonlocal conversations_inserted, messages_inserted
            conversations_inserted += self._conversations.bulk_insert_ignore_conflicts(
                conversation_batch
            )
            # Conversations flush before their messages, so FKs always resolve.
            messages_inserted += self._messages.bulk_insert_ignore_conflicts(message_batch)
            conversation_batch.clear()
            message_batch.clear()

        for raw in records:
            row = conversation_row(raw)
            conversation_batch.append(row)
            batch = message_rows(raw, row["id"])
            message_batch.extend(batch)
            messages_seen += len(batch)
            if len(message_batch) >= _CHUNK_SIZE:
                flush()
        flush()

        self._session.commit()
        return IngestionResult(
            conversations_seen=len(records),
            conversations_inserted=conversations_inserted,
            messages_seen=messages_seen,
            messages_inserted=messages_inserted,
        )
