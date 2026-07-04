"""Deterministic context construction for the Resolution Assistant.

Everything here is deterministic — no AI reasoning (the only AI touchpoint is
the query embedding inside the existing retrieval service). The context
builder renders the retrieval query from the current issue, calls the Phase 5
``retrieve()`` unchanged, and packages the hits into a ContextBundle with
stable citation ids. Citation validation and the zero-hit guard keep the
grounding policy enforceable in code rather than trusting the model.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..knowledge_base.retrieval import retrieve
from ..knowledge_base.schema import KnowledgeDocument
from ..llm import EmbeddingProvider
from ..understanding.schema import Issue
from .schema import ContextBundle, ContextDocument, ResolutionResponse, RetrievalMetadata

NO_EVIDENCE_RECOMMENDATION = "No sufficiently similar historical resolutions were found."

_DOWNGRADE_NOTE = (
    " [Downgraded by the platform: the response cited no retrieved documents, "
    "so it cannot be treated as grounded.]"
)


def render_issue_query(issue: Issue) -> str:
    """Render the retrieval query for one issue.

    Kept symmetric with ``render_knowledge_text`` (same labels and joiners for
    the fields knowable before resolution) so the query lives in the same
    embedding space as the documents; the customer's wording is appended as
    extra semantic signal. Empty sections are omitted.
    """
    lines = [f"Problem: {issue.canonical_name}."]
    if issue.product:
        lines.append(f"Product: {issue.product}.")
    if issue.symptoms:
        lines.append(f"Symptoms: {'; '.join(issue.symptoms)}.")
    lines.append(f"Customer description: {issue.customer_description}.")
    return "\n".join(lines)


def build_context(
    session: Session,
    embedder: EmbeddingProvider,
    issue: Issue,
    *,
    limit: int = 5,
) -> ContextBundle:
    """Gather the current issue, retrieved evidence, and retrieval metadata."""
    query = render_issue_query(issue)
    product = issue.product or None
    hits = retrieve(session, embedder, query, product=product, limit=limit)
    # retrieve() filters by exact product match and relaxes only when the
    # filtered search returns nothing — so any hit with a different product
    # proves the filter was relaxed.
    filter_relaxed = product is not None and any(h.product != product for h in hits)
    return ContextBundle(
        issue=issue,
        documents=[
            ContextDocument(
                doc_id=f"KB-{rank}",
                conversation_id=hit.conversation_id,
                distance=hit.distance,
                document=KnowledgeDocument.model_validate(hit.document),
            )
            for rank, hit in enumerate(hits, start=1)
        ],
        retrieval=RetrievalMetadata(
            query_text=query,
            product_filter=product,
            filter_relaxed=filter_relaxed,
            limit=limit,
            result_count=len(hits),
            distances=[hit.distance for hit in hits],
        ),
    )


def ungrounded_response(reason: str) -> ResolutionResponse:
    """The deterministic no-evidence answer — a successful outcome, not an error."""
    return ResolutionResponse(
        recommendation=NO_EVIDENCE_RECOMMENDATION,
        reasoning=reason,
        recommended_actions=[],
        grounded=False,
        evidence_strength="none",
        citations=[],
    )


def validate_citations(response: ResolutionResponse, bundle: ContextBundle) -> ResolutionResponse:
    """Deterministically enforce the grounding policy on one LLM response.

    Citations must reference documents that were actually supplied; a
    "grounded" response with no valid citations is downgraded rather than
    trusted, and an ungrounded response must not smuggle in actions.
    """
    valid_ids = {doc.doc_id for doc in bundle.documents}
    citations: list[str] = []
    for citation in response.citations:
        if citation in valid_ids and citation not in citations:
            citations.append(citation)

    if response.grounded and not citations:
        return response.model_copy(
            update={
                "grounded": False,
                "evidence_strength": "none",
                "recommended_actions": [],
                "citations": [],
                "reasoning": response.reasoning + _DOWNGRADE_NOTE,
            }
        )
    if not response.grounded:
        return response.model_copy(update={"citations": [], "recommended_actions": []})
    return response.model_copy(update={"citations": citations})
