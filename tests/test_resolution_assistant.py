"""DB-backed tests for the Resolution Assistant service and CLI (fake provider)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cxintel.knowledge_base.service import KnowledgeBaseService
from cxintel.resolution_assistant.context import NO_EVIDENCE_RECOMMENDATION
from cxintel.resolution_assistant.schema import ResolutionResponse
from cxintel.resolution_assistant.service import (
    ConversationNotFoundError,
    NoIssuesFoundError,
    ResolutionAssistantService,
    UnknownIssueIndexError,
)
from cxintel.understanding.schema import StructuredConversation

from .test_knowledge_base import FakeEmbedder, seed_analysis, seed_knowledge_scenario
from .test_knowledge_generation import make_issue, make_structured


def grounded_response(citations: list[str] | None = None) -> ResolutionResponse:
    return ResolutionResponse(
        recommendation="Replace the base seal.",
        reasoning="KB-1 resolved the same leak by replacing the seal.",
        recommended_actions=["Ship a replacement base seal."],
        grounded=True,
        evidence_strength="strong",
        citations=citations if citations is not None else ["KB-1"],
    )


class FakeProvider:
    """Schema-dispatching provider: Prompt #1 → structured, Prompt #2 → response."""

    def __init__(
        self,
        structured: StructuredConversation | None = None,
        response: ResolutionResponse | None = None,
    ) -> None:
        self.structured = structured
        self.response = response or grounded_response()
        self.calls: list[tuple[str, type]] = []

    def extract(self, prompt: str, schema: type, on_retry: Any = None) -> Any:
        self.calls.append((prompt, schema))
        if schema is StructuredConversation:
            assert self.structured is not None, "unexpected Prompt #1 call"
            return self.structured
        assert schema is ResolutionResponse
        return self.response


@pytest.fixture
def factory(settings_on_test_db: str, migrated_engine: Any, db_session: Session) -> Any:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False)


@pytest.fixture
def kb(factory: Any, db_session: Session) -> Session:
    seed_knowledge_scenario(db_session)
    KnowledgeBaseService(factory, FakeEmbedder()).run()
    return db_session


def make_service(factory: Any, provider: FakeProvider) -> ResolutionAssistantService:
    return ResolutionAssistantService(factory, provider, FakeEmbedder())


def seed_open_leak(session: Session, external_id: str = "res_open_leak") -> uuid.UUID:
    """A new, unresolved leak conversation with a persisted analysis."""
    structured = make_structured(
        [make_issue("base water leak", resolution_status="unresolved", resolution_summary=None)],
        resolved=False,
        resolution_type=None,
    )
    seed_analysis(session, external_id, structured)
    return uuid.uuid5(uuid.NAMESPACE_URL, external_id)


# --- conversation mode ------------------------------------------------------------


def test_resolve_conversation_by_external_id(factory: Any, kb: Session) -> None:
    seed_open_leak(kb)
    provider = FakeProvider()
    result = make_service(factory, provider).resolve_conversation("res_open_leak")

    assert result.source == "conversation"
    assert result.conversation_id == uuid.uuid5(uuid.NAMESPACE_URL, "res_open_leak")
    assert result.llm_called is True
    assert result.response.grounded is True
    assert result.response.citations == ["KB-1"]
    assert result.bundle.documents[0].document.issue == "base water leak"

    # Exactly one LLM call (Prompt #2), fed the bundle — never the transcript.
    assert len(provider.calls) == 1
    prompt, schema = provider.calls[0]
    assert schema is ResolutionResponse
    assert '"doc_id": "KB-1"' in prompt
    assert "Conversation transcript" not in prompt


def test_resolve_conversation_by_uuid(factory: Any, kb: Session) -> None:
    conv_id = seed_open_leak(kb)
    result = make_service(factory, FakeProvider()).resolve_conversation(str(conv_id))
    assert result.conversation_id == conv_id


def test_unknown_conversation(factory: Any, kb: Session) -> None:
    with pytest.raises(ConversationNotFoundError, match="No conversation found"):
        make_service(factory, FakeProvider()).resolve_conversation("nope-404")


def test_conversation_without_analysis(factory: Any, kb: Session) -> None:
    from .test_understanding import seed_conversation

    seed_conversation(kb, "res_unanalyzed", 2, "pod is broken")
    with pytest.raises(ConversationNotFoundError, match="no analysis yet"):
        make_service(factory, FakeProvider()).resolve_conversation("res_unanalyzed")


# --- issue selection ---------------------------------------------------------------


def seed_two_issue_conversation(session: Session) -> None:
    structured = make_structured(
        [
            make_issue("base water leak"),  # resolved
            make_issue(
                "wifi connectivity drop",
                resolution_status="unresolved",
                resolution_summary=None,
                product="Hub 2",
            ),
        ]
    )
    seed_analysis(session, "res_two_issues", structured)


def test_default_selection_prefers_first_unresolved_issue(factory: Any, kb: Session) -> None:
    seed_two_issue_conversation(kb)
    result = make_service(factory, FakeProvider()).resolve_conversation("res_two_issues")
    assert result.selected_issue_index == 1
    assert result.bundle.issue.canonical_name == "wifi connectivity drop"
    assert [o.index for o in result.issues] == [0, 1]
    assert result.issues[0].resolution_status == "resolved"


def test_explicit_issue_index_is_honored(factory: Any, kb: Session) -> None:
    seed_two_issue_conversation(kb)
    result = make_service(factory, FakeProvider()).resolve_conversation(
        "res_two_issues", issue_index=0
    )
    assert result.selected_issue_index == 0
    assert result.bundle.issue.canonical_name == "base water leak"


def test_out_of_range_issue_index(factory: Any, kb: Session) -> None:
    seed_two_issue_conversation(kb)
    with pytest.raises(UnknownIssueIndexError, match="out of range"):
        make_service(factory, FakeProvider()).resolve_conversation(
            "res_two_issues", issue_index=5
        )


def test_conversation_with_no_issues(factory: Any, kb: Session) -> None:
    seed_analysis(kb, "res_no_issues", make_structured([]))
    with pytest.raises(NoIssuesFoundError):
        make_service(factory, FakeProvider()).resolve_conversation("res_no_issues")


def test_conversation_issues_listing(factory: Any, kb: Session) -> None:
    seed_two_issue_conversation(kb)
    options = make_service(factory, FakeProvider()).conversation_issues("res_two_issues")
    assert [o.canonical_name for o in options] == ["base water leak", "wifi connectivity drop"]


# --- zero-hit guard ----------------------------------------------------------------


def test_empty_knowledge_base_answers_without_llm_call(
    factory: Any, db_session: Session
) -> None:
    seed_open_leak(db_session)
    provider = FakeProvider()
    result = make_service(factory, provider).resolve_conversation("res_open_leak")

    assert result.llm_called is False
    assert provider.calls == []  # the LLM was never invoked
    assert result.response.grounded is False
    assert result.response.recommendation == NO_EVIDENCE_RECOMMENDATION
    assert result.response.evidence_strength == "none"


# --- grounding enforcement ----------------------------------------------------------


def test_fabricated_citations_downgrade_the_response(factory: Any, kb: Session) -> None:
    seed_open_leak(kb)
    provider = FakeProvider(response=grounded_response(citations=["KB-99"]))
    result = make_service(factory, provider).resolve_conversation("res_open_leak")
    assert result.response.grounded is False
    assert result.response.citations == []
    assert "Downgraded by the platform" in result.response.reasoning


# --- ticket mode --------------------------------------------------------------------


def ticket_structured() -> StructuredConversation:
    return make_structured(
        [make_issue("base water leak", resolution_status="unresolved", resolution_summary=None)],
        resolved=False,
        resolution_type=None,
    )


def test_resolve_ticket_runs_prompt_1_then_prompt_2(factory: Any, kb: Session) -> None:
    from cxintel.repositories import ConversationAnalysisRepository, ConversationRepository

    conversations_before = ConversationRepository(kb).count()
    analyses_before = ConversationAnalysisRepository(kb).count()

    provider = FakeProvider(structured=ticket_structured())
    result = make_service(factory, provider).resolve_ticket(
        "my pod is leaking water everywhere", product="Pod 5"
    )

    assert result.source == "ticket"
    assert result.conversation_id is None
    assert result.response.grounded is True

    assert [schema for _, schema in provider.calls] == [
        StructuredConversation,
        ResolutionResponse,
    ]
    prompt_1 = provider.calls[0][0]
    assert "[customer] my pod is leaking water everywhere" in prompt_1
    assert "product=Pod 5" in prompt_1

    # Interactive tickets persist nothing.
    assert ConversationRepository(kb).count() == conversations_before
    assert ConversationAnalysisRepository(kb).count() == analyses_before


def test_resolve_ticket_defaults_product_metadata(factory: Any, kb: Session) -> None:
    provider = FakeProvider(structured=ticket_structured())
    make_service(factory, provider).resolve_ticket("pod leaking")
    assert "product=unknown" in provider.calls[0][0]


# --- CLI -----------------------------------------------------------------------------


@pytest.fixture
def cli_env(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeProvider]:
    from cxintel.config import get_settings

    provider = FakeProvider(structured=ticket_structured())
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    get_settings.cache_clear()
    monkeypatch.setattr("cxintel.llm.get_llm_provider", lambda **kw: provider)
    monkeypatch.setattr("cxintel.llm.get_embedding_provider", lambda: FakeEmbedder())
    yield provider
    get_settings.cache_clear()


def test_cli_chat_ticket_mode(kb: Session, cli_env: FakeProvider) -> None:
    from typer.testing import CliRunner

    from cxintel.cli import app as cli_app

    result = CliRunner().invoke(
        cli_app, ["chat", "my pod is leaking water", "--product", "Pod 5"]
    )
    assert result.exit_code == 0, result.output
    assert "GROUNDED" in result.output
    assert "Replace the base seal." in result.output
    assert "KB-1" in result.output


def test_cli_chat_conversation_mode(kb: Session, cli_env: FakeProvider) -> None:
    from typer.testing import CliRunner

    from cxintel.cli import app as cli_app

    seed_open_leak(kb)
    result = CliRunner().invoke(cli_app, ["chat", "--conversation", "res_open_leak"])
    assert result.exit_code == 0, result.output
    assert "GROUNDED" in result.output


def test_cli_chat_rejects_text_and_conversation_together(
    kb: Session, cli_env: FakeProvider
) -> None:
    from typer.testing import CliRunner

    from cxintel.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["chat", "text", "--conversation", "x"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
