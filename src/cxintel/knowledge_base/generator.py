"""Deterministic KnowledgeDocument generation (ADR-014 — no LLM).

Conversation Understanding already performed the semantic reasoning; this
module only reshapes the Structured Conversation Object into retrieval
artifacts. Pure functions: fully testable, perfectly reproducible.
"""

from __future__ import annotations

from ..understanding.schema import StructuredConversation
from .schema import KnowledgeDocument


def knowledge_documents(structured: StructuredConversation) -> list[KnowledgeDocument]:
    """One KnowledgeDocument per successfully resolved issue.

    Unresolved, in-progress, and escalated issues are intentionally excluded —
    the knowledge base only teaches from what actually worked.
    """
    resolution = structured.resolution
    outcome = "resolved"
    if resolution.requires_replacement:
        outcome = "resolved — hardware replacement still outstanding"
    return [
        KnowledgeDocument(
            issue=issue.canonical_name,
            product=issue.product,
            symptoms=issue.symptoms,
            resolution_type=resolution.resolution_type,
            resolution_summary=issue.resolution_summary or resolution.summary,
            actions=resolution.actions,
            outcome=outcome,
        )
        for issue in structured.issues
        if issue.resolution_status == "resolved"
    ]
