"""The canonical KnowledgeDocument — the Phase 5 retrieval artifact.

One successfully resolved Issue produces one KnowledgeDocument (unresolved
work is intentionally excluded from the knowledge base). The document is
derived entirely from the Structured Conversation Object by deterministic
code — no LLM is involved (ADR-014). It is persisted as JSONB alongside its
rendered ``knowledge_text`` and embedding in ``knowledge_documents``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KnowledgeDocument(BaseModel):
    """Reusable operational knowledge distilled from one resolved issue."""

    issue: str = Field(description="Canonical issue category name.")
    customer_description: str = Field(
        default="",
        description="The customer's own wording for the problem, preserved verbatim.",
    )
    product: str = Field(description="The product the issue concerns ('' when unknown).")
    symptoms: list[str] = Field(description="Observable symptoms reported by the customer.")
    prerequisites: list[str] = Field(
        default_factory=list,
        description=(
            "Diagnostics or preconditions verified before the resolution. Empty "
            "under schema V1 — the Structured Conversation Object does not yet "
            "capture diagnostics separately from actions."
        ),
    )
    resolution_type: str | None = Field(
        description="Resolution category (e.g. 'replacement', 'troubleshooting')."
    )
    resolution_summary: str = Field(description="How the issue was resolved.")
    actions: list[str] = Field(description="Concrete actions taken by the agent or customer.")
    outcome: str = Field(description="Final state of the issue (always a resolved outcome).")
