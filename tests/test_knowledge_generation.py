"""Pure unit tests for deterministic KnowledgeDocument generation (no DB, no LLM)."""

from __future__ import annotations

from cxintel.knowledge_base.generator import knowledge_documents
from cxintel.knowledge_base.rendering import render_knowledge_text
from cxintel.knowledge_base.schema import KnowledgeDocument
from cxintel.understanding.schema import (
    CatalogMatch,
    ConversationMeta,
    Issue,
    Resolution,
    StructuredConversation,
    Summary,
)


def make_issue(
    name: str = "base water leak",
    *,
    resolution_status: str = "resolved",
    resolution_summary: str | None = "replaced the base seal",
    symptoms: list[str] | None = None,
    product: str = "Pod 5",
) -> Issue:
    return Issue(
        canonical_name=name,
        customer_description=f"customer says {name}",
        severity="medium",
        confidence=0.9,
        customer_impact="high",
        product=product,
        symptoms=symptoms if symptoms is not None else ["water pooling under the base"],
        catalog=CatalogMatch(matched=True, confidence=0.9),
        resolution_status=resolution_status,
        resolution_summary=resolution_summary,
    )


def make_structured(
    issues: list[Issue],
    *,
    resolved: bool = True,
    resolution_type: str | None = "troubleshooting",
    actions: list[str] | None = None,
    requires_replacement: bool = False,
) -> StructuredConversation:
    return StructuredConversation(
        summary=Summary(short="short", detailed="detailed"),
        issues=issues,
        resolution=Resolution(
            resolved=resolved,
            resolution_type=resolution_type,
            summary="agent walked the customer through a fix",
            actions=actions if actions is not None else ["checked the valve"],
            requires_replacement=requires_replacement,
        ),
        conversation=ConversationMeta(
            language="English",
            multiple_issues=len(issues) > 1,
            requires_followup=False,
            customer_emotion="calm",
            analysis_confidence=0.9,
        ),
    )


# --- generation -----------------------------------------------------------------


def test_one_resolved_issue_produces_one_document() -> None:
    structured = make_structured([make_issue()])
    docs = knowledge_documents(structured)
    assert len(docs) == 1
    doc = docs[0]
    assert isinstance(doc, KnowledgeDocument)
    assert doc.issue == "base water leak"
    assert doc.product == "Pod 5"
    assert doc.symptoms == ["water pooling under the base"]
    assert doc.resolution_type == "troubleshooting"
    assert doc.resolution_summary == "replaced the base seal"
    assert doc.actions == ["checked the valve"]
    assert doc.outcome == "resolved"


def test_unresolved_issues_are_excluded() -> None:
    structured = make_structured(
        [
            make_issue("leak", resolution_status="resolved"),
            make_issue("wifi drop", resolution_status="unresolved", resolution_summary=None),
            make_issue("app crash", resolution_status="in_progress", resolution_summary=None),
            make_issue("billing", resolution_status="escalated", resolution_summary=None),
        ]
    )
    docs = knowledge_documents(structured)
    assert [d.issue for d in docs] == ["leak"]


def test_fully_unresolved_conversation_produces_no_documents() -> None:
    structured = make_structured(
        [make_issue(resolution_status="unresolved", resolution_summary=None)],
        resolved=False,
        resolution_type=None,
    )
    assert knowledge_documents(structured) == []


def test_issue_without_own_summary_falls_back_to_conversation_resolution() -> None:
    structured = make_structured([make_issue(resolution_summary=None)])
    docs = knowledge_documents(structured)
    assert docs[0].resolution_summary == "agent walked the customer through a fix"


def test_outstanding_replacement_reflected_in_outcome() -> None:
    structured = make_structured([make_issue()], requires_replacement=True)
    docs = knowledge_documents(structured)
    assert "replacement" in docs[0].outcome


def test_generation_is_deterministic() -> None:
    structured = make_structured([make_issue("a"), make_issue("b")])
    assert knowledge_documents(structured) == knowledge_documents(structured)


# --- knowledge_text rendering ------------------------------------------------------


def test_rendered_text_contains_all_populated_sections() -> None:
    doc = knowledge_documents(make_structured([make_issue()]))[0]
    text = render_knowledge_text(doc)
    assert "base water leak" in text
    assert "Pod 5" in text
    assert "water pooling under the base" in text
    assert "replaced the base seal" in text
    assert "troubleshooting" in text
    assert "checked the valve" in text


def test_rendered_text_omits_empty_sections() -> None:
    doc = KnowledgeDocument(
        issue="wifi drop",
        product="",
        symptoms=[],
        prerequisites=[],
        resolution_type=None,
        resolution_summary="router restarted",
        actions=[],
        outcome="resolved",
    )
    text = render_knowledge_text(doc)
    assert "wifi drop" in text
    assert "router restarted" in text
    assert "Product" not in text
    assert "Symptoms" not in text
    assert "Resolution type" not in text
    assert "Actions" not in text


def test_rendered_text_reads_naturally() -> None:
    doc = knowledge_documents(make_structured([make_issue()]))[0]
    text = render_knowledge_text(doc)
    # Prose sections, not JSON — and stable across calls.
    assert "{" not in text and "}" not in text
    assert text == render_knowledge_text(doc)
    assert text.startswith("Problem:")
