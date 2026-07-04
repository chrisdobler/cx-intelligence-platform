"""Tests for the deterministic Phase 6 context builder and grounding enforcement."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from cxintel.resolution_assistant.context import (
    NO_EVIDENCE_RECOMMENDATION,
    build_context,
    render_issue_query,
    ungrounded_response,
    validate_citations,
)
from cxintel.resolution_assistant.schema import (
    ContextBundle,
    ContextDocument,
    ResolutionResponse,
    RetrievalMetadata,
)

from .test_knowledge_base import FakeEmbedder, seed_knowledge_scenario
from .test_knowledge_generation import make_issue


@pytest.fixture
def kb(factory: Any, db_session: Session) -> Session:
    """A knowledge base built from the shared three-conversation scenario."""
    from cxintel.knowledge_base.service import KnowledgeBaseService

    seed_knowledge_scenario(db_session)
    KnowledgeBaseService(factory, FakeEmbedder()).run()
    return db_session


@pytest.fixture
def factory(settings_on_test_db: str, migrated_engine: Any, db_session: Session) -> Any:
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=migrated_engine, expire_on_commit=False)


# --- query rendering -------------------------------------------------------------


def test_query_rendering_is_deterministic_and_symmetric_with_knowledge_text() -> None:
    issue = make_issue("base water leak", symptoms=["water pooling under the base"])
    query = render_issue_query(issue)
    assert query == render_issue_query(issue)  # deterministic
    assert query.splitlines() == [
        "Problem: base water leak.",
        "Product: Pod 5.",
        "Symptoms: water pooling under the base.",
        "Customer description: customer says base water leak.",
    ]


def test_query_rendering_omits_empty_sections() -> None:
    issue = make_issue("base water leak", symptoms=[], product="")
    query = render_issue_query(issue)
    assert "Product:" not in query
    assert "Symptoms:" not in query


# --- context building ------------------------------------------------------------


def test_build_context_assigns_citation_ids_in_rank_order(kb: Session) -> None:
    issue = make_issue("base water leak")
    bundle = build_context(kb, FakeEmbedder(), issue, limit=5)

    assert [d.doc_id for d in bundle.documents] == [
        f"KB-{i}" for i in range(1, len(bundle.documents) + 1)
    ]
    assert bundle.documents[0].document.issue == "base water leak"
    assert bundle.issue == issue
    assert bundle.retrieval.query_text == render_issue_query(issue)
    assert bundle.retrieval.product_filter == "Pod 5"
    assert bundle.retrieval.filter_relaxed is False
    assert bundle.retrieval.limit == 5
    assert bundle.retrieval.result_count == len(bundle.documents)
    assert bundle.retrieval.distances == [d.distance for d in bundle.documents]


def test_build_context_reports_filter_relaxation(kb: Session) -> None:
    issue = make_issue("base water leak", product="Nonexistent 9")
    bundle = build_context(kb, FakeEmbedder(), issue, limit=2)

    assert bundle.documents  # relaxation found evidence anyway
    assert bundle.retrieval.product_filter == "Nonexistent 9"
    assert bundle.retrieval.filter_relaxed is True


def test_build_context_on_empty_knowledge_base(
    factory: Any, db_session: Session
) -> None:
    bundle = build_context(db_session, FakeEmbedder(), make_issue(), limit=5)
    assert bundle.documents == []
    assert bundle.retrieval.result_count == 0


# --- zero-hit guard --------------------------------------------------------------


def test_ungrounded_response_is_a_valid_success_shape() -> None:
    response = ungrounded_response("nothing retrieved")
    assert response.recommendation == NO_EVIDENCE_RECOMMENDATION
    assert response.grounded is False
    assert response.evidence_strength == "none"
    assert response.recommended_actions == []
    assert response.citations == []
    assert response.reasoning == "nothing retrieved"


# --- citation validation ---------------------------------------------------------


def _bundle_with_ids(*doc_ids: str) -> ContextBundle:
    import uuid

    from cxintel.knowledge_base.schema import KnowledgeDocument

    doc = KnowledgeDocument(
        issue="base water leak",
        product="Pod 5",
        symptoms=["water pooling"],
        resolution_type="replacement",
        resolution_summary="replaced the base seal",
        actions=["shipped a replacement base"],
        outcome="resolved",
    )
    return ContextBundle(
        issue=make_issue(),
        documents=[
            ContextDocument(
                doc_id=doc_id,
                conversation_id=uuid.uuid5(uuid.NAMESPACE_URL, doc_id),
                distance=0.1,
                document=doc,
            )
            for doc_id in doc_ids
        ],
        retrieval=RetrievalMetadata(
            query_text="q",
            product_filter=None,
            filter_relaxed=False,
            limit=5,
            result_count=len(doc_ids),
            distances=[0.1] * len(doc_ids),
        ),
    )


def _response(**overrides: Any) -> ResolutionResponse:
    values: dict[str, Any] = {
        "recommendation": "replace the base",
        "reasoning": "KB-1 matches",
        "recommended_actions": ["ship a replacement base"],
        "grounded": True,
        "evidence_strength": "strong",
        "citations": ["KB-1"],
    }
    values.update(overrides)
    return ResolutionResponse(**values)


def test_unknown_citations_are_dropped_and_deduped() -> None:
    bundle = _bundle_with_ids("KB-1", "KB-2")
    validated = validate_citations(
        _response(citations=["KB-2", "KB-9", "KB-2", "KB-1"]), bundle
    )
    assert validated.citations == ["KB-2", "KB-1"]  # order preserved, dedup, KB-9 gone
    assert validated.grounded is True


def test_grounded_response_without_valid_citations_is_downgraded() -> None:
    bundle = _bundle_with_ids("KB-1")
    validated = validate_citations(_response(citations=["KB-7"]), bundle)
    assert validated.grounded is False
    assert validated.evidence_strength == "none"
    assert validated.recommended_actions == []
    assert validated.citations == []
    assert "Downgraded by the platform" in validated.reasoning


def test_ungrounded_response_cannot_smuggle_actions_or_citations() -> None:
    bundle = _bundle_with_ids("KB-1")
    validated = validate_citations(
        _response(grounded=False, citations=["KB-1"], recommended_actions=["try this"]),
        bundle,
    )
    assert validated.grounded is False
    assert validated.citations == []
    assert validated.recommended_actions == []


def test_validation_does_not_mutate_the_original_response() -> None:
    bundle = _bundle_with_ids("KB-1")
    original = _response(citations=["KB-9"])
    validate_citations(original, bundle)
    assert original.citations == ["KB-9"]
    assert original.grounded is True
