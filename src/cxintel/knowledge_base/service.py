"""Knowledge base generation — Phase 5 pipeline stage business logic.

Deterministic end to end except for the embedding call (ADR-014): every
analyzed conversation's Structured Conversation Object is reshaped into
KnowledgeDocuments (resolved issues only), rendered to knowledge_text, and
embedded into pgvector. Documents are derived data regenerated per
conversation — but reruns are cheap: a conversation whose documents are
byte-identical to what is already stored is skipped without any embedding
call, and changed conversations reuse embeddings for any unchanged text.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from ..llm import EmbeddingProvider
from ..models import KnowledgeDocumentRecord
from ..pipeline.progress import ProgressCallback, ProgressReporter
from ..repositories import ConversationAnalysisRepository, KnowledgeDocumentRepository
from ..understanding.schema import StructuredConversation
from .generator import knowledge_documents
from .rendering import render_knowledge_text
from .schema import KnowledgeDocument

logger = logging.getLogger(__name__)


def _noop_progress(_message: object) -> None:
    return None


class KnowledgeBaseResult:
    """Outcome of one knowledge-base generation run."""

    def __init__(self) -> None:
        self.conversations_processed = 0
        self.conversations_skipped = 0
        self.documents = 0
        self.documents_embedded = 0
        self.failures = 0
        self.no_analyses = False

    def summary(self) -> str:
        if self.no_analyses:
            return "No analyzed conversations — run Conversation Understanding first."
        parts = [
            f"Knowledge base: {self.documents} documents from "
            f"{self.conversations_processed} conversations "
            f"({self.documents_embedded} embedded, "
            f"{self.conversations_skipped} conversations unchanged)."
        ]
        if self.failures:
            parts.append(f"{self.failures} conversation(s) failed to load.")
        return " ".join(parts)


@dataclass
class _PendingConversation:
    """One conversation whose documents changed and must be re-persisted."""

    conversation_id: uuid.UUID
    documents: list[tuple[KnowledgeDocument, str]]  # (document, knowledge_text)
    reusable_embeddings: dict[str, object] = field(default_factory=dict)


class KnowledgeBaseService:
    """Builds the retrieval knowledge base from persisted conversation analyses."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedder: EmbeddingProvider,
        *,
        pipeline_run_id: uuid.UUID | None = None,
    ) -> None:
        from ..config import get_settings

        self._session_factory = session_factory
        self._embedder = embedder
        self._pipeline_run_id = pipeline_run_id
        self._embedding_model = get_settings().embedding_model

    def run(
        self, progress: ProgressCallback | ProgressReporter = _noop_progress
    ) -> KnowledgeBaseResult:
        reporter = (
            progress
            if isinstance(progress, ProgressReporter)
            else ProgressReporter(
                stage_key="knowledge_base",
                stage_label="Knowledge Base",
                progress=progress,
                message="Preparing knowledge base generation…",
            )
        )
        result = KnowledgeBaseResult()

        with self._session_factory() as session:
            conversation_ids = ConversationAnalysisRepository(session).conversation_ids()
        if not conversation_ids:
            result.no_analyses = True
            reporter.report(message=result.summary())
            return result

        reporter.report(
            total_work=len(conversation_ids),
            message=f"Generating knowledge documents for {len(conversation_ids)} conversations…",
        )
        pending = self._collect_pending(conversation_ids, result, reporter)
        embeddings = self._embed_new_texts(pending, reporter)
        self._persist(pending, embeddings, result, reporter)
        reporter.report(message=result.summary())
        return result

    # -- phase A: deterministic generation + change detection ---------------------

    def _collect_pending(
        self,
        conversation_ids: list[uuid.UUID],
        result: KnowledgeBaseResult,
        reporter: ProgressReporter,
    ) -> list[_PendingConversation]:
        pending: list[_PendingConversation] = []
        for conversation_id in conversation_ids:
            with self._session_factory() as session:
                analysis = ConversationAnalysisRepository(session).get(conversation_id)
                existing = KnowledgeDocumentRepository(session).for_conversation(conversation_id)
            if analysis is None:  # pragma: no cover - deleted between queries
                continue
            try:
                structured = StructuredConversation.model_validate(analysis.analysis_json)
            except ValidationError as exc:
                logger.warning("analysis for %s failed to validate: %s", conversation_id, exc)
                result.failures += 1
                reporter.advance(current_item=str(conversation_id), failed=True)
                continue

            documents = [
                (doc, render_knowledge_text(doc)) for doc in knowledge_documents(structured)
            ]
            existing_by_text = {row.knowledge_text: row.embedding for row in existing}
            if sorted(text for _, text in documents) == sorted(existing_by_text):
                result.conversations_skipped += 1
                result.documents += len(documents)
                reporter.advance(current_item=str(conversation_id))
                continue
            pending.append(
                _PendingConversation(
                    conversation_id=conversation_id,
                    documents=documents,
                    reusable_embeddings=dict(existing_by_text),
                )
            )
        return pending

    # -- phase B: embed only what is new ------------------------------------------

    def _embed_new_texts(
        self, pending: list[_PendingConversation], reporter: ProgressReporter
    ) -> dict[str, object]:
        texts: list[str] = []
        seen: set[str] = set()
        for item in pending:
            for _, text in item.documents:
                if text not in item.reusable_embeddings and text not in seen:
                    seen.add(text)
                    texts.append(text)
        if not texts:
            return {}
        reporter.report(message=f"Embedding {len(texts)} knowledge documents…")
        vectors = self._embedder.embed_documents(texts)
        return dict(zip(texts, vectors, strict=True))

    # -- phase C: persist per conversation ------------------------------------------

    def _persist(
        self,
        pending: list[_PendingConversation],
        embeddings: dict[str, object],
        result: KnowledgeBaseResult,
        reporter: ProgressReporter,
    ) -> None:
        now = datetime.now(tz=UTC)
        for item in pending:
            rows = []
            for doc, text in item.documents:
                embedding = item.reusable_embeddings.get(text)
                if embedding is None:
                    embedding = embeddings[text]
                    result.documents_embedded += 1
                rows.append(
                    KnowledgeDocumentRecord(
                        id=uuid.uuid4(),
                        conversation_id=item.conversation_id,
                        issue=doc.issue,
                        product=doc.product,
                        document=doc.model_dump(mode="json"),
                        knowledge_text=text,
                        embedding=embedding,
                        embedding_model=self._embedding_model,
                        created_at=now,
                    )
                )
            with self._session_factory() as session:
                KnowledgeDocumentRepository(session).replace_for_conversation(
                    item.conversation_id, rows
                )
                session.commit()
            result.conversations_processed += 1
            result.documents += len(rows)
            reporter.advance(current_item=str(item.conversation_id))
        result.conversations_processed += result.conversations_skipped
