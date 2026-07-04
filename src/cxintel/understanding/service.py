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
analysis are skipped, and conversations with terminal recorded failures are
skipped until an explicit retry-failures run. Exhausted transient failures
remain pending for a later run. Invalid data is never persisted.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy.orm import Session, sessionmaker

from ..llm import LLMExtractionError, LLMProvider
from ..models import (
    Conversation,
    ConversationAnalysis,
    ConversationIssue,
    IssueCatalogEntry,
    LLMCallObservation,
)
from ..pipeline.progress import ProgressCallback, ProgressReporter
from ..repositories import (
    ConversationIssueRepository,
    ConversationRepository,
    ConversationUnderstandingFailureRepository,
    IssueCatalogRepository,
    LLMCallObservationRepository,
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
        self.skipped_terminal_failures = 0
        self.failed = 0
        self.retryable_failed = 0
        self.permanent_failed = 0
        self.catalog_entries: int | None = None
        self.observed_calls = 0
        self.total_seconds = 0.0
        self.llm_seconds = 0.0
        self.retry_count = 0
        self.slowest_conversation: str | None = None
        self.slowest_seconds = 0.0

    def summary(self) -> str:
        failure_parts = [
            f"{self.retryable_failed} retryable",
            f"{self.permanent_failed} permanent",
        ]
        parts = [
            f"Analyzed {self.analyzed} conversations "
            f"({self.failed} failed: {', '.join(failure_parts)}; "
            f"{self.skipped_existing} already analyzed; "
            f"{self.skipped_terminal_failures} terminal failures skipped)."
        ]
        if self.observed_calls:
            avg_total = self.total_seconds / self.observed_calls
            avg_llm = self.llm_seconds / self.observed_calls
            parts.append(
                f"Timing: avg {avg_total:.2f}s total, {avg_llm:.2f}s LLM, "
                f"{self.retry_count} retries"
                + (
                    f", slowest {self.slowest_conversation} {self.slowest_seconds:.2f}s."
                    if self.slowest_conversation
                    else "."
                )
            )
        if self.catalog_entries is not None:
            parts.append(f"Issue catalog: {self.catalog_entries} entries.")
        return " ".join(parts)

    def observe(self, timing: ObservationTiming) -> None:
        self.observed_calls += 1
        self.total_seconds += timing.total_seconds
        self.llm_seconds += timing.llm_seconds
        self.retry_count += timing.retry_count
        if timing.total_seconds >= self.slowest_seconds:
            self.slowest_seconds = timing.total_seconds
            self.slowest_conversation = timing.item


@dataclass(frozen=True)
class ObservationTiming:
    """Small aggregate copied into the run summary."""

    item: str
    total_seconds: float
    llm_seconds: float
    retry_count: int


class UnderstandingService:
    """Runs Conversation Understanding over all pending conversations."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: LLMProvider,
        concurrency: int | None = None,
        pipeline_run_id: uuid.UUID | None = None,
    ) -> None:
        from ..config import get_settings

        settings = get_settings()
        self._session_factory = session_factory
        self._provider = provider
        self._concurrency = concurrency or settings.understand_concurrency
        self._model = settings.llm_model
        self._pipeline_run_id = pipeline_run_id
        self._counter_lock = Lock()

    def run(
        self,
        limit: int | None = None,
        progress: ProgressCallback | ProgressReporter = _noop_progress,
        *,
        retry_failures: bool = False,
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
            total_work = self._pending_work_count(
                conversations, days, limit, retry_failures=retry_failures
            )

        reporter.report(
            total_work=total_work,
            completed_work=0,
            succeeded_work=0,
            message=(
                f"Retrying {total_work} failed conversations…"
                if retry_failures and total_work
                else f"Understanding {total_work} pending conversations…"
                if total_work
                else "No recorded failures to retry."
                if retry_failures
                else "No pending conversations to understand."
            ),
        )

        remaining = limit
        for day in days:
            if remaining is not None and remaining <= 0:
                break
            processed = self._run_day(
                day, remaining, result, reporter, retry_failures=retry_failures
            )
            if remaining is not None:
                remaining -= processed

        self._maybe_build_catalog(days, result, reporter)
        return result

    def _pending_work_count(
        self,
        conversations: ConversationRepository,
        days: list[int],
        limit: int | None,
        *,
        retry_failures: bool,
    ) -> int:
        remaining = limit
        total = 0
        for day in days:
            if remaining is not None and remaining <= 0:
                break
            pending = (
                conversations.terminal_failure_ids_for_day(day, remaining)
                if retry_failures
                else conversations.pending_analysis_ids_for_day(day, remaining)
            )
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
        *,
        retry_failures: bool,
    ) -> int:
        with self._session_factory() as session:
            conversations = ConversationRepository(session)
            pending = (
                conversations.terminal_failure_ids_for_day(day, limit)
                if retry_failures
                else conversations.pending_analysis_ids_for_day(day, limit)
            )
            result.skipped_existing += conversations.analyzed_count_for_day(day)
            if not retry_failures:
                result.skipped_terminal_failures += conversations.terminal_failure_count_for_day(
                    day
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
                self._process_one(conversation_id, day, catalog_context, reporter, item, result)
                with self._counter_lock:
                    result.analyzed += 1
            except LLMExtractionError as exc:
                logger.warning("understanding failed for %s: %s", conversation_id, exc)
                failed = True
                with self._counter_lock:
                    result.failed += 1
                    if exc.retryable:
                        result.retryable_failed += 1
                    else:
                        result.permanent_failed += 1
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
        result: UnderstandingResult,
    ) -> None:
        """Extract, validate, and persist one conversation (own session)."""
        observation_started_at = datetime.now(tz=UTC)
        observation_started = time.perf_counter()
        load_seconds = 0.0
        prompt_seconds = 0.0
        llm_seconds = 0.0
        persist_seconds = 0.0
        message_count = 0
        prompt_characters = 0
        issue_count = 0
        retry_count = 0
        status = "succeeded"
        error: str | None = None
        analysis: StructuredConversation | None = None

        with self._session_factory() as session:
            phase_started = time.perf_counter()
            conversation = session.get(Conversation, conversation_id)
            assert conversation is not None  # id came from the pending query
            messages = list(conversation.messages)
            message_count = len(messages)
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
            load_seconds = time.perf_counter() - phase_started

            phase_started = time.perf_counter()
            prompt = build_prompt(conversation, messages, catalog, seen_names)
            prompt_seconds = time.perf_counter() - phase_started
            prompt_characters = len(prompt)

        try:
            phase_started = time.perf_counter()

            def on_retry(attempt: int, _exc: Exception) -> None:
                nonlocal retry_count
                retry_count += 1
                reporter.retry(
                    current_item=item,
                    message=f"Retrying {item} (attempt {attempt})…",
                )

            analysis = self._provider.extract(
                prompt,
                StructuredConversation,
                on_retry=on_retry,
            )
            llm_seconds = time.perf_counter() - phase_started
            issue_count = len(analysis.issues)

            now = datetime.now(tz=UTC)
            with self._session_factory() as session:
                phase_started = time.perf_counter()
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
                ConversationUnderstandingFailureRepository(session).clear(conversation_id)
                session.commit()
                persist_seconds = time.perf_counter() - phase_started
        except Exception as exc:
            status = "failed"
            error = str(exc)
            if llm_seconds == 0.0:
                llm_seconds = time.perf_counter() - phase_started
            if isinstance(exc, LLMExtractionError) and not exc.retryable:
                self._record_terminal_failure(
                    conversation_id=conversation_id,
                    day=day,
                    category=exc.category,
                    error=str(exc),
                    retry_count=retry_count,
                    failed_at=datetime.now(tz=UTC),
                )
            raise
        finally:
            total_seconds = time.perf_counter() - observation_started
            finished_at = datetime.now(tz=UTC)
            self._record_observation(
                LLMCallObservation(
                    id=uuid.uuid4(),
                    pipeline_run_id=self._pipeline_run_id,
                    conversation_id=conversation_id,
                    day=day,
                    model=self._model,
                    prompt_version=PROMPT_VERSION,
                    status=status,
                    total_seconds=total_seconds,
                    load_seconds=load_seconds,
                    prompt_seconds=prompt_seconds,
                    llm_seconds=llm_seconds,
                    persist_seconds=persist_seconds,
                    message_count=message_count,
                    prompt_characters=prompt_characters,
                    issue_count=issue_count,
                    retry_count=retry_count,
                    started_at=observation_started_at,
                    finished_at=finished_at,
                    error=error,
                )
            )
            with self._counter_lock:
                result.observe(
                    ObservationTiming(
                        item=item,
                        total_seconds=total_seconds,
                        llm_seconds=llm_seconds,
                        retry_count=retry_count,
                    )
                )

    def _record_observation(self, observation: LLMCallObservation) -> None:
        """Persist observation data without making instrumentation a hard dependency."""
        try:
            with self._session_factory() as session:
                LLMCallObservationRepository(session).add(observation)
                session.commit()
        except Exception as exc:
            logger.warning(
                "failed to record LLM observation for %s: %s",
                observation.conversation_id,
                exc,
            )

    def _record_terminal_failure(
        self,
        *,
        conversation_id: uuid.UUID,
        day: int,
        category: str,
        error: str,
        retry_count: int,
        failed_at: datetime,
    ) -> None:
        """Persist permanent failures so normal reruns can resume missing work."""
        try:
            with self._session_factory() as session:
                ConversationUnderstandingFailureRepository(session).upsert(
                    conversation_id=conversation_id,
                    pipeline_run_id=self._pipeline_run_id,
                    day=day,
                    model=self._model,
                    prompt_version=PROMPT_VERSION,
                    status="terminal",
                    failure_category=category,
                    error=error,
                    retry_count=retry_count,
                    failed_at=failed_at,
                )
                session.commit()
        except Exception as exc:
            logger.warning(
                "failed to record terminal understanding failure for %s: %s",
                conversation_id,
                exc,
            )

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
            if conversations.pending_analysis_ids_for_day(
                baseline_day, limit=1, include_terminal_failures=True
            ):
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
