"""Phase 6 schemas — ContextBundle (assistant input) and ResolutionResponse (output).

The ContextBundle is assembled deterministically by the context builder and is
the *complete* input to Prompt #2 — the assistant never sees raw conversations
and never performs retrieval. The ResolutionResponse is the assistant's typed
AI contract (ADR-009): its schema is supplied to the provider natively and
every response is validated before it reaches application code.

Field descriptions matter: they flow into the provider-generated JSON Schema
and are the only structure-adjacent guidance the model receives.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from ..knowledge_base.schema import KnowledgeDocument
from ..understanding.schema import Issue, ResolutionStatus

EvidenceStrength = Literal["none", "weak", "moderate", "strong"]


class RetrievalMetadata(BaseModel):
    """How the evidence was retrieved — deterministic provenance, no AI."""

    query_text: str = Field(description="The exact text embedded as the retrieval query.")
    product_filter: str | None = Field(
        description="Product metadata filter applied before semantic search (null = none)."
    )
    filter_relaxed: bool = Field(
        description=(
            "True when the product filter matched nothing and was relaxed "
            "before the semantic search."
        )
    )
    limit: int = Field(description="Maximum number of documents requested.")
    result_count: int = Field(description="Number of documents actually retrieved.")
    distances: list[float] = Field(
        description="Cosine distance per retrieved document, in rank order (lower is closer)."
    )


class ContextDocument(BaseModel):
    """One retrieved KnowledgeDocument, tagged with a stable citation id."""

    doc_id: str = Field(
        description="Stable citation id assigned in retrieval rank order: 'KB-1', 'KB-2', …"
    )
    conversation_id: uuid.UUID = Field(
        description="The resolved conversation this knowledge document was distilled from."
    )
    distance: float = Field(description="Cosine distance to the query (lower is closer).")
    document: KnowledgeDocument = Field(
        description="The canonical KnowledgeDocument (Phase 5 artifact)."
    )


class ContextBundle(BaseModel):
    """The complete, deterministic input to the Resolution Assistant (Prompt #2)."""

    issue: Issue = Field(
        description="The current issue, verbatim from the StructuredConversation."
    )
    documents: list[ContextDocument] = Field(
        description="Retrieved historical evidence, in rank order (closest first)."
    )
    retrieval: RetrievalMetadata = Field(description="Retrieval provenance.")


class ResolutionResponse(BaseModel):
    """Grounded decision-support output of the Resolution Assistant."""

    recommendation: str = Field(
        description=(
            "The single best evidence-supported resolution path, or an explicit "
            "statement that no sufficiently similar historical resolutions were found."
        )
    )
    reasoning: str = Field(
        description="Brief explanation of why the cited evidence supports the recommendation."
    )
    recommended_actions: list[str] = Field(
        description=(
            "Concrete ordered actions, each traceable to cited evidence. "
            "Empty when the response is not grounded."
        )
    )
    grounded: bool = Field(
        description=(
            "True only when the recommendation is fully supported by the "
            "supplied knowledge documents."
        )
    )
    evidence_strength: EvidenceStrength = Field(
        description="Strength of the supporting historical evidence."
    )
    citations: list[str] = Field(
        description="doc_id values ('KB-n') of the documents that support the recommendation."
    )


class IssueOption(BaseModel):
    """One selectable issue from a structured conversation (for issue pickers)."""

    index: int = Field(description="Position of the issue in the conversation's issues list.")
    canonical_name: str = Field(description="Canonical issue category name.")
    product: str = Field(description="The product the issue concerns.")
    resolution_status: ResolutionStatus = Field(
        description="Resolution state of the issue at the end of the conversation."
    )
    customer_description: str = Field(description="The customer's own wording for the problem.")


class ResolutionResult(BaseModel):
    """Everything one resolution request produced (service output = API body)."""

    source: Literal["conversation", "ticket"] = Field(
        description="Whether the issue came from a stored conversation or a free-text ticket."
    )
    conversation_id: uuid.UUID | None = Field(
        description="The source conversation id (null in ticket mode)."
    )
    issues: list[IssueOption] = Field(
        description="All issues found in the conversation, for issue selection."
    )
    selected_issue_index: int = Field(description="Index of the issue that was resolved.")
    bundle: ContextBundle = Field(description="The deterministic context the assistant saw.")
    response: ResolutionResponse = Field(description="The assistant's (validated) response.")
    llm_called: bool = Field(
        description="False when the zero-hit guard answered without an LLM call."
    )
