"""Conversation Understanding — Phase 3 pipeline stage business logic.

Interprets every imported conversation exactly once (ADR-008): the provider
extracts a canonical StructuredConversation, which is persisted unchanged to
``ConversationAnalysis.analysis_json`` and projected 1:1 into
``conversation_issues``. Days are processed in order with a strict barrier
between them: the Issue Catalog is derived from Day 1 only (ADR-011), and
Days 2+ normalize against that frozen baseline. Within a day, a small worker
pool bounds wall-clock time — outputs are identical to sequential processing,
only the order of API calls differs.

Runs are resumable and idempotent: conversations that already have an
analysis are skipped, so a sample run followed by a full run (or an
interrupted full run) simply continues where it left off. A conversation
whose extraction fails is recorded and skipped — invalid data is never
persisted (nothing is written for it at all).
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy.orm import Session, sessionmaker

from ..llm import LLMExtractionError, LLMProvider
from ..models import Conversation, ConversationAnalysis, ConversationIssue, IssueCatalogEntry
from ..pipeline.progress import ProgressCallback, ProgressReporter
from ..repositories import (
    ConversationIssueRepository,
    ConversationRepository,
    IssueCatalogRepository,
)
from .prompt import PROMPT_VERSION, build_prompt
from .schema import StructuredConversation

logger = logging.getLogger(__name__)

_REPRESENTATIVE_EXAMPLES = 3


def _noop_progress(_message: object) -> None:
    return None


class UnderstandingResult:
    """Outcome of one understanding run."""

    def __init__(self) -> None:
        self.analyzed = 0
        self.skipped_existing = 0
        self.failed = 0
        self.catalog_entries: int | None = None

    def summary(self) -> str:
        parts = [
            f"Analyzed {self.analyzed} conversations "
            f"({self.failed} failed, {self.skipped_existing} already analyzed)."
        ]
        if self.catalog_entries is not None:
            parts.append(f"Issue catalog: {self.catalog_entries} entries.")
        return " ".join(parts)


class UnderstandingService:
    """Runs Conversation Understanding over all pending conversations."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: LLMProvider,
        concurrency: int | None = None,
    ) -> None:
        from ..config import get_settings

        settings = get_settings()
        self._session_factory = session_factory
        self._provider = provider
        self._concurrency = concurrency or settings.understand_concurrency
        self._model = settings.llm_model
        self._counter_lock = Lock()

    def run(
        self,
        limit: int | None = None,
        progress: ProgressCallback | ProgressReporter = _noop_progress,
    ) -> UnderstandingResult:
        """Process pending conversations day by day (day boundaries are barriers)."""
        reporter = (
            progress
            if isinstance(progress, ProgressReporter)
            else ProgressReporter(
                stage_key="understand",
                stage_label="Conversation Understanding",
                progress=progress,
                message="Preparing conversation understanding…",
            )
        )
        result = UnderstandingResult()

        with self._session_factory() as session:
            conversations = ConversationRepository(session)
            days = conversations.days()
            total_work = self._pending_work_count(conversations, days, limit)

        reporter.report(
            total_work=total_work,
            completed_work=0,
            message=(
                f"Understanding {total_work} pending conversations…"
                if total_work
                else "No pending conversations to understand."
            ),
        )

        remaining = limit
        for day in days:
            if remaining is not None and remaining <= 0:
                break
            processed = self._run_day(day, remaining, result, reporter)
            if remaining is not None:
                remaining -= processed

        self._maybe_build_catalog(days, result, reporter)
        return result

    def _pending_work_count(
        self, conversations: ConversationRepository, days: list[int], limit: int | None
    ) -> int:
        remaining = limit
        total = 0
        for day in days:
            if remaining is not None and remaining <= 0:
                break
            pending = conversations.pending_analysis_ids_for_day(day, remaining)
            total += len(pending)
            if remaining is not None:
                remaining -= len(pending)
        return total

    # -- per-day processing ---------------------------------------------------

    def _run_day(
        self,
        day: int,
        limit: int | None,
        result: UnderstandingResult,
        reporter: ProgressReporter,
    ) -> int:
        with self._session_factory() as session:
            conversations = ConversationRepository(session)
            total_for_day = conversations.count_for_day(day)
            pending = conversations.pending_analysis_ids_for_day(day, limit)
            result.skipped_existing += total_for_day - len(
                conversations.pending_analysis_ids_for_day(day)
            )
            catalog = IssueCatalogRepository(session).all() if day > 1 else []
            catalog_context = [(e.canonical_name, e.description) for e in catalog]

        if not pending:
            return 0

        done = 0

        def work(conversation_id: uuid.UUID) -> None:
            nonlocal done
            item = self._conversation_label(conversation_id)
            reporter.set_current(item, message=f"Day {day}: analyzing {item}…")
            failed = False
            try:
                self._process_one(conversation_id, day, catalog_context, reporter, item)
                with self._counter_lock:
                    result.analyzed += 1
            except LLMExtractionError as exc:
                logger.warning("understanding failed for %s: %s", conversation_id, exc)
                failed = True
                with self._counter_lock:
                    result.failed += 1
            with self._counter_lock:
                done += 1
                day_done = done
            reporter.advance(
                current_item=item,
                failed=failed,
                message=f"Day {day}: {day_done}/{len(pending)} processed.",
            )

        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            list(pool.map(work, pending))

        reporter.report(message=f"Day {day}: {done}/{len(pending)} processed.")
        return done

    def _conversation_label(self, conversation_id: uuid.UUID) -> str:
        with self._session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            return conversation.external_id if conversation is not None else str(conversation_id)

    def _process_one(
        self,
        conversation_id: uuid.UUID,
        day: int,
        catalog_context: list[tuple[str, str]],
        reporter: ProgressReporter,
        item: str,
    ) -> None:
        """Extract, validate, and persist one conversation (own session)."""
        with self._session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            assert conversation is not None  # id came from the pending query
            messages = conversation.messages
            catalog = [
                IssueCatalogEntry(
                    canonical_name=name,
                    description=description,
                    first_seen_day=1,
                    example_count=0,
                    representative_examples=[],
                    created_at=datetime.now(tz=UTC),
                )
                for name, description in catalog_context
            ]
            seen_names = (
                ConversationIssueRepository(session).canonical_names_for_day(day)
                if not catalog
                else None
            )
            prompt = build_prompt(conversation, messages, catalog, seen_names)

        analysis = self._provider.extract(
            prompt,
            StructuredConversation,
            on_retry=lambda attempt, _exc: reporter.retry(
                current_item=item,
                message=f"Retrying {item} (attempt {attempt})…",
            ),
        )

        now = datetime.now(tz=UTC)
        with self._session_factory() as session:
            session.merge(
                ConversationAnalysis(
                    conversation_id=conversation_id,
                    model=self._model,
                    # Response-level version reporting arrives with Phase 7
                    # observability; the configured model is the fallback.
                    model_version=self._model,
                    prompt_version=PROMPT_VERSION,
                    processed_at=now,
                    analysis_json=analysis.model_dump(),
                )
            )
            ConversationIssueRepository(session).replace_for_conversation(
                conversation_id,
                [
                    ConversationIssue(
                        id=uuid.uuid4(),
                        conversation_id=conversation_id,
                        canonical_name=issue.canonical_name,
                        customer_description=issue.customer_description,
                        severity=issue.severity,
                        confidence=issue.confidence,
                        customer_impact=issue.customer_impact,
                        product=issue.product,
                        symptoms=issue.symptoms,
                        catalog_matched=issue.catalog.matched,
                        catalog_confidence=issue.catalog.confidence,
                        resolution_status=issue.resolution_status,
                        resolution_summary=issue.resolution_summary,
                        created_at=now,
                    )
                    for issue in analysis.issues
                ],
            )
            session.commit()

    # -- baseline catalog -----------------------------------------------------

    def _maybe_build_catalog(
        self, days: list[int], result: UnderstandingResult, reporter: ProgressReporter
    ) -> None:
        """(Re)build the Day-1 baseline catalog once Day 1 is fully analyzed."""
        if not days:
            return
        baseline_day = days[0]
        with self._session_factory() as session:
            conversations = ConversationRepository(session)
            if conversations.pending_analysis_ids_for_day(baseline_day, limit=1):
                return  # baseline incomplete — catalog not built yet
            issues = ConversationIssueRepository(session)
            now = datetime.now(tz=UTC)
            entries = [
                IssueCatalogEntry(
                    canonical_name=agg.canonical_name,
                    description=agg.examples[0],
                    first_seen_day=baseline_day,
                    example_count=agg.example_count,
                    representative_examples=agg.examples[:_REPRESENTATIVE_EXAMPLES],
                    created_at=now,
                )
                for agg in sorted(
                    issues.aggregate_for_day(baseline_day), key=lambda a: a.canonical_name
                )
            ]
            IssueCatalogRepository(session).replace_all(entries)
            session.commit()
            result.catalog_entries = len(entries)
        reporter.report(
            message=f"Issue catalog rebuilt from Day {baseline_day}: {len(entries)} entries."
        )
