"""Resolution Assistant service — the one place the Phase 6 flow is wired.

Flow: obtain a StructuredConversation (persisted analysis, or Prompt #1 over a
free-text ticket) → select one issue → deterministic context building →
Prompt #2 → deterministic citation validation. The CLI, the REST API, and the
control center all call this service, so the business logic exists once.

Interactive by design: nothing here is persisted — free-text tickets do not
create conversations or analyses, and responses are not stored. Batch-stage
observability (``llm_call_observations``) intentionally does not apply to
interactive calls; per-call observability is Phase 7 work.
"""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from ..llm import EmbeddingProvider, LLMProvider
from ..models import Conversation, Message
from ..repositories import (
    ConversationAnalysisRepository,
    ConversationRepository,
    IssueCatalogRepository,
)
from ..understanding.prompt import build_prompt
from ..understanding.schema import Issue, StructuredConversation
from .context import build_context, ungrounded_response, validate_citations
from .prompt import build_resolution_prompt
from .schema import IssueOption, ResolutionResponse, ResolutionResult


class ConversationNotFoundError(Exception):
    """The referenced conversation does not exist or has no persisted analysis."""


class NoIssuesFoundError(Exception):
    """The structured conversation contains no issues to resolve."""


class UnknownIssueIndexError(Exception):
    """The requested issue index is out of range."""


class ResolutionAssistantService:
    """Grounded decision support over the knowledge base (Phase 6)."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: LLMProvider,
        embedder: EmbeddingProvider,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._embedder = embedder

    # --- public API ---------------------------------------------------------

    def resolve_conversation(
        self, conversation_ref: str, *, issue_index: int | None = None, limit: int = 5
    ) -> ResolutionResult:
        """Recommend a resolution for one issue of an already-analyzed conversation."""
        with self._session_factory() as session:
            conversation_id, structured = self._load_structured(session, conversation_ref)
            return self._resolve(
                session,
                structured,
                source="conversation",
                conversation_id=conversation_id,
                issue_index=issue_index,
                limit=limit,
            )

    def resolve_ticket(
        self,
        text: str,
        *,
        product: str | None = None,
        issue_index: int | None = None,
        limit: int = 5,
    ) -> ResolutionResult:
        """Recommend a resolution for a free-text new ticket.

        The ticket is structured with the existing Prompt #1 (the assistant
        never reinterprets conversations itself) and nothing is persisted.
        """
        with self._session_factory() as session:
            structured = self._structure_ticket(session, text, product)
            return self._resolve(
                session,
                structured,
                source="ticket",
                conversation_id=None,
                issue_index=issue_index,
                limit=limit,
            )

    def conversation_issues(self, conversation_ref: str) -> list[IssueOption]:
        """The selectable issues of one analyzed conversation (for pickers)."""
        with self._session_factory() as session:
            _, structured = self._load_structured(session, conversation_ref)
        return _issue_options(structured.issues)

    # --- internals ----------------------------------------------------------

    def _resolve(
        self,
        session: Session,
        structured: StructuredConversation,
        *,
        source: Literal["conversation", "ticket"],
        conversation_id: uuid.UUID | None,
        issue_index: int | None,
        limit: int,
    ) -> ResolutionResult:
        selected = _select_issue(structured.issues, issue_index)
        issue = structured.issues[selected]
        bundle = build_context(session, self._embedder, issue, limit=limit)

        if not bundle.documents:
            response = ungrounded_response(
                "The knowledge base returned no documents for this issue "
                f"(query: {bundle.retrieval.query_text!r}). "
                "A grounded recommendation is not possible."
            )
            llm_called = False
        else:
            raw = self._provider.extract(build_resolution_prompt(bundle), ResolutionResponse)
            response = validate_citations(raw, bundle)
            llm_called = True

        return ResolutionResult(
            source=source,
            conversation_id=conversation_id,
            issues=_issue_options(structured.issues),
            selected_issue_index=selected,
            bundle=bundle,
            response=response,
            llm_called=llm_called,
        )

    def _load_structured(
        self, session: Session, conversation_ref: str
    ) -> tuple[uuid.UUID, StructuredConversation]:
        conversation = _find_conversation(session, conversation_ref)
        if conversation is None:
            raise ConversationNotFoundError(f"No conversation found for '{conversation_ref}'.")
        analysis = ConversationAnalysisRepository(session).get(conversation.id)
        if analysis is None:
            raise ConversationNotFoundError(
                f"Conversation '{conversation_ref}' has no analysis yet — "
                "run 'app understand' first."
            )
        return conversation.id, StructuredConversation.model_validate(analysis.analysis_json)

    def _structure_ticket(
        self, session: Session, text: str, product: str | None
    ) -> StructuredConversation:
        # Transient stand-ins for Prompt #1's metadata line and transcript —
        # never added to the session, never persisted.
        conversation = Conversation(
            product=product or "unknown",
            category="unknown",
            priority="unknown",
            status="open",
        )
        messages = [Message(role="customer", body=text)]
        catalog = IssueCatalogRepository(session).all()
        prompt = build_prompt(conversation, messages, catalog)
        return self._provider.extract(prompt, StructuredConversation)


def _find_conversation(session: Session, conversation_ref: str) -> Conversation | None:
    try:
        conversation_id = uuid.UUID(conversation_ref)
    except ValueError:
        return ConversationRepository(session).get_by_external_id(conversation_ref)
    return session.get(Conversation, conversation_id)


def _select_issue(issues: list[Issue], issue_index: int | None) -> int:
    """The explicitly requested issue, else the first unresolved one, else the first."""
    if not issues:
        raise NoIssuesFoundError("The conversation contains no issues to resolve.")
    if issue_index is not None:
        if not 0 <= issue_index < len(issues):
            raise UnknownIssueIndexError(
                f"Issue index {issue_index} is out of range (0..{len(issues) - 1})."
            )
        return issue_index
    for index, issue in enumerate(issues):
        if issue.resolution_status != "resolved":
            return index
    return 0


def _issue_options(issues: list[Issue]) -> list[IssueOption]:
    return [
        IssueOption(
            index=index,
            canonical_name=issue.canonical_name,
            product=issue.product,
            resolution_status=issue.resolution_status,
            customer_description=issue.customer_description,
        )
        for index, issue in enumerate(issues)
    ]
