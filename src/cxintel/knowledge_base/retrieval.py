"""Metadata-first semantic retrieval over the knowledge base.

Stage 1 applies deterministic metadata filters (every stored document is
already resolved-only by construction; ``product`` narrows further). Stage 2
ranks the remaining candidates by pgvector cosine distance. When the filters
leave no candidates, they are progressively relaxed before the semantic
search runs — a filtered miss should not mean an empty answer.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..llm import EmbeddingProvider
from ..repositories import KnowledgeDocumentRepository


class RetrievedKnowledge(BaseModel):
    """One knowledge-base hit, ready for context building (Phase 6)."""

    conversation_id: uuid.UUID
    issue: str
    product: str
    knowledge_text: str
    document: dict[str, Any] = Field(description="The canonical KnowledgeDocument JSON.")
    distance: float = Field(description="Cosine distance to the query (lower is closer).")


def retrieve(
    session: Session,
    embedder: EmbeddingProvider,
    query: str,
    *,
    product: str | None = None,
    limit: int = 5,
) -> list[RetrievedKnowledge]:
    """Metadata-filtered semantic search with progressive filter relaxation."""
    repo = KnowledgeDocumentRepository(session)
    if repo.count() == 0:
        return []
    embedding = embedder.embed_query(query)
    hits = repo.search(embedding, product=product, limit=limit)
    if not hits and product is not None:
        hits = repo.search(embedding, product=None, limit=limit)  # relax metadata
    return [
        RetrievedKnowledge(
            conversation_id=row.conversation_id,
            issue=row.issue,
            product=row.product,
            knowledge_text=row.knowledge_text,
            document=row.document,
            distance=distance,
        )
        for row, distance in hits
    ]
