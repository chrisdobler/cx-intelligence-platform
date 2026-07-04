"""DB-backed tests for the Conversation Understanding service (fake provider)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from threading import Lock
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from cxintel.config import get_settings
from cxintel.llm import (
    LLMFailureCategory,
    PermanentLLMExtractionError,
    RetryableLLMExtractionError,
    RetryCallback,
)
from cxintel.models import Conversation, ConversationIssue, Message, PipelineRun
from cxintel.repositories import (
    ConversationAnalysisRepository,
    ConversationIssueRepository,
    ConversationUnderstandingFailureRepository,
    IssueCatalogRepository,
    LLMCallObservationRepository,
)
from cxintel.understanding.prompt import PROMPT_VERSION
from cxintel.understanding.schema import StructuredConversation
from cxintel.understanding.service import UnderstandingService

from .test_understanding_schema import FROZEN_V1_EXAMPLE

T = TypeVar("T", bound=BaseModel)


def analysis_for(issue_names: list[str], *, matched: bool = True) -> StructuredConversation:
    base = StructuredConversation.model_validate(FROZEN_V1_EXAMPLE)
    template = base.issues[0]
    issues = [
        template.model_copy(
            update={
                "canonical_name": name,
                "customer_description": f"customer says {name}",
                "catalog": template.catalog.model_copy(update={"matched": matched}),
            }
        )
        for name in issue_names
    ]
    return base.model_copy(update={"issues": issues})


class FakeProvider:
    """Maps a marker embedded in the transcript to a canned analysis."""

    def __init__(self, by_marker: dict[str, StructuredConversation | Exception]) -> None:
        self.by_marker = by_marker
        self.prompts: list[str] = []
        self._lock = Lock()

    def extract(
        self, prompt: str, schema: type[T], on_retry: RetryCallback | None = None
    ) -> T:
        with self._lock:
            self.prompts.append(prompt)
        for marker, result in self.by_marker.items():
            if marker in prompt:
                if isinstance(result, Exception):
                    raise result
                return cast(T, result)
        raise AssertionError(f"no canned analysis matches prompt: {prompt[:200]}")


class RetryingProvider(FakeProvider):
    """Fake provider that reports retries before returning the canned result."""

    def __init__(
        self, by_marker: dict[str, StructuredConversation | Exception], retries: int
    ) -> None:
        super().__init__(by_marker)
        self.retries = retries

    def extract(
        self, prompt: str, schema: type[T], on_retry: RetryCallback | None = None
    ) -> T:
        for attempt in range(2, self.retries + 2):
            if on_retry is not None:
                on_retry(attempt, RuntimeError("try again"))
        return super().extract(prompt, schema, on_retry)


def seed_conversation(session: Session, external_id: str, day: int, text: str) -> uuid.UUID:
    ts = datetime(2026, 2, 24 + day, 12, 0, tzinfo=UTC)
    conv_id = uuid.uuid5(uuid.NAMESPACE_URL, external_id)
    session.add(
        Conversation(
            id=conv_id,
            external_id=external_id,
            customer_id="cust_x",
            status="resolved",
            priority="medium",
            category="hardware",
            issue_type="leak",
            product="Pod 5",
            day=day,
            started_at=ts,
            ended_at=ts,
            created_at=ts,
            updated_at=ts,
        )
    )
    session.add(
        Message(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"{external_id}_m1"),
            external_id=f"{external_id}_m1",
            conversation_id=conv_id,
            role="customer",
            body=text,
            created_at=ts,
        )
    )
    session.commit()
    return conv_id


@pytest.fixture
def factory(settings_on_test_db: str, migrated_engine: Any, db_session: Session) -> Any:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False)


def make_service(
    factory: Any,
    provider: FakeProvider,
    pipeline_run_id: uuid.UUID | None = None,
    concurrency: int = 1,
) -> UnderstandingService:
    return UnderstandingService(
        factory, provider, concurrency=concurrency, pipeline_run_id=pipeline_run_id
    )


def seed_pipeline_run(session: Session) -> uuid.UUID:
    run_id = uuid.uuid4()
    ts = datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    session.add(
        PipelineRun(
            id=run_id,
            stage_key="understand",
            status="running",
            trigger="cli",
            started_at=ts,
            finished_at=None,
            duration_seconds=None,
            summary=None,
            error=None,
        )
    )
    session.commit()
    return run_id


def test_analysis_persisted_unchanged_with_provenance(
    factory: Any, db_session: Session
) -> None:
    conv_id = seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    canned = analysis_for(["base water leak"])
    provider = FakeProvider({"marker-alpha": canned})

    result = make_service(factory, provider).run(limit=None)
    assert result.analyzed == 1
    assert result.failed == 0

    stored = ConversationAnalysisRepository(db_session).get(conv_id)
    assert stored is not None
    # Canonical artifact persisted unchanged.
    assert stored.analysis_json == canned.model_dump()
    assert stored.model == "gemini-2.5-flash"
    assert stored.model_version
    assert stored.prompt_version == PROMPT_VERSION
    assert stored.processed_at is not None


def test_successful_extraction_records_llm_observation(
    factory: Any, db_session: Session
) -> None:
    conv_id = seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    run_id = seed_pipeline_run(db_session)
    provider = FakeProvider({"marker-alpha": analysis_for(["base water leak"])})

    result = make_service(factory, provider, pipeline_run_id=run_id).run(limit=None)

    observations = LLMCallObservationRepository(db_session).slowest()
    assert len(observations) == 1
    observation = observations[0]
    assert result.observed_calls == 1
    assert result.retry_count == 0
    assert "Timing: avg" in result.summary()
    assert observation.pipeline_run_id == run_id
    assert observation.conversation_id == conv_id
    assert observation.status == "succeeded"
    assert observation.message_count == 1
    assert observation.prompt_characters == len(provider.prompts[0])
    assert observation.issue_count == 1
    assert observation.total_seconds >= observation.llm_seconds >= 0
    assert observation.load_seconds >= 0
    assert observation.prompt_seconds >= 0
    assert observation.persist_seconds >= 0


def test_failed_extraction_records_observation_without_analysis(
    factory: Any, db_session: Session
) -> None:
    conv_id = seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    provider = FakeProvider({"marker-alpha": PermanentLLMExtractionError("no valid response")})

    result = make_service(factory, provider).run(limit=None)

    assert result.failed == 1
    assert result.permanent_failed == 1
    assert ConversationAnalysisRepository(db_session).count() == 0
    observations = LLMCallObservationRepository(db_session).slowest()
    assert len(observations) == 1
    assert observations[0].conversation_id == conv_id
    assert observations[0].pipeline_run_id is None
    assert observations[0].status == "failed"
    assert observations[0].error is not None and "no valid response" in observations[0].error
    failure = ConversationUnderstandingFailureRepository(db_session).get(conv_id)
    assert failure is not None
    assert failure.failure_category == LLMFailureCategory.PERMANENT_API
    assert failure.error == "no valid response"


def test_retry_callbacks_increment_observation_retry_count(
    factory: Any, db_session: Session
) -> None:
    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    provider = RetryingProvider({"marker-alpha": analysis_for(["leak"])}, retries=2)

    result = make_service(factory, provider).run(limit=None)

    observations = LLMCallObservationRepository(db_session).slowest()
    assert result.retry_count == 2
    assert observations[0].retry_count == 2


def test_issue_projection_one_row_per_issue(factory: Any, db_session: Session) -> None:
    conv_id = seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    provider = FakeProvider({"marker-alpha": analysis_for(["leak", "noise"])})
    make_service(factory, provider).run(limit=None)

    issues = db_session.query(ConversationIssue).all()
    assert {i.canonical_name for i in issues} == {"leak", "noise"}
    for issue in issues:
        assert issue.conversation_id == conv_id
        assert issue.customer_description.startswith("customer says")
        assert issue.severity == "high"
        assert issue.catalog_matched is True


def test_zero_issue_conversation_persists_analysis_only(
    factory: Any, db_session: Session
) -> None:
    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    provider = FakeProvider({"marker-alpha": analysis_for([])})
    make_service(factory, provider).run(limit=None)
    assert ConversationAnalysisRepository(db_session).count() == 1
    assert ConversationIssueRepository(db_session).count() == 0


def test_catalog_built_from_day1_only_and_fed_to_day2(
    factory: Any, db_session: Session
) -> None:
    seed_conversation(db_session, "conv_d1", 1, "marker-day1")
    seed_conversation(db_session, "conv_d2", 2, "marker-day2")
    provider = FakeProvider(
        {
            "marker-day1": analysis_for(["zeta baseline flux regulator"]),
            "marker-day2": analysis_for(["totally novel problem"], matched=False),
        }
    )
    result = make_service(factory, provider).run(limit=None)
    assert result.analyzed == 2

    catalog = IssueCatalogRepository(db_session).all()
    # Catalog derives from Day 1 only — the novel Day-2 issue is NOT added.
    assert [e.canonical_name for e in catalog] == ["zeta baseline flux regulator"]
    entry = catalog[0]
    assert entry.first_seen_day == 1
    assert entry.example_count == 1
    assert entry.representative_examples == ["customer says zeta baseline flux regulator"]

    # The Day-2 prompt received the Day-1 catalog for normalization.
    day2_prompt = next(p for p in provider.prompts if "marker-day2" in p)
    assert "zeta baseline flux regulator" in day2_prompt

    # The novel Day-2 issue surfaces as a candidate novel issue.
    assert ConversationIssueRepository(db_session).unmatched_count() == 1


def test_baseline_incomplete_defers_later_days(factory: Any, db_session: Session) -> None:
    seed_conversation(db_session, "conv_d1", 1, "marker-day1")
    seed_conversation(db_session, "conv_d2", 2, "marker-day2")
    provider = FakeProvider(
        {
            "marker-day1": PermanentLLMExtractionError("baseline failed"),
            "marker-day2": analysis_for(["day2 should wait"]),
        }
    )

    result = make_service(factory, provider).run(limit=None)

    assert result.failed == 1
    assert result.analyzed == 0
    assert ConversationAnalysisRepository(db_session).count() == 0
    assert IssueCatalogRepository(db_session).count() == 0
    assert len(provider.prompts) == 1
    assert "marker-day2" not in provider.prompts[0]


def test_high_concurrency_keeps_one_request_and_projection_per_conversation(
    factory: Any, db_session: Session
) -> None:
    total = 40
    by_marker: dict[str, StructuredConversation | Exception] = {}
    for i in range(total):
        marker = f"marker-{i:02d}-end"
        seed_conversation(db_session, f"conv_{i:02d}", 1, marker)
        by_marker[marker] = analysis_for([f"issue {i:02d}"])
    provider = FakeProvider(by_marker)
    run_id = seed_pipeline_run(db_session)

    result = make_service(
        factory, provider, pipeline_run_id=run_id, concurrency=32
    ).run(limit=None)

    assert result.analyzed == total
    assert result.failed == 0
    assert len(provider.prompts) == total
    for marker in by_marker:
        assert sum(marker in prompt for prompt in provider.prompts) == 1
    assert ConversationAnalysisRepository(db_session).count() == total
    assert LLMCallObservationRepository(db_session).count() == total
    issues = db_session.query(ConversationIssue).all()
    assert len(issues) == total
    assert len({issue.conversation_id for issue in issues}) == total


def test_rerun_skips_already_analyzed(factory: Any, db_session: Session) -> None:
    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    provider = FakeProvider({"marker-alpha": analysis_for(["leak"])})
    service = make_service(factory, provider)
    assert service.run(limit=None).analyzed == 1

    rerun = service.run(limit=None)
    assert rerun.analyzed == 0
    assert rerun.skipped_existing == 1
    assert len(provider.prompts) == 1  # provider not called again


def test_selected_model_applies_only_to_new_analyses(
    factory: Any, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_id = seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    provider = FakeProvider({"marker-alpha": analysis_for(["leak"])})
    assert make_service(factory, provider).run(limit=None).analyzed == 1

    second_id = seed_conversation(db_session, "conv_b", 1, "marker-beta")
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-flash-lite")
    get_settings.cache_clear()
    try:
        continuation = FakeProvider(
            {
                "marker-alpha": AssertionError("already analyzed conversation was reprocessed"),
                "marker-beta": analysis_for(["sensor drift"]),
            }
        )
        result = make_service(factory, continuation).run(limit=None)
    finally:
        get_settings.cache_clear()

    assert result.analyzed == 1
    assert result.skipped_existing == 1
    assert len(continuation.prompts) == 1
    first = ConversationAnalysisRepository(db_session).get(first_id)
    second = ConversationAnalysisRepository(db_session).get(second_id)
    assert first is not None
    assert second is not None
    assert first.model == "gemini-2.5-flash"
    assert first.model_version == "gemini-2.5-flash"
    assert second.model == "gemini-2.5-flash-lite"
    assert second.model_version == "gemini-2.5-flash-lite"


def test_normal_rerun_skips_recorded_terminal_failures(
    factory: Any, db_session: Session
) -> None:
    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    provider = FakeProvider({"marker-alpha": PermanentLLMExtractionError("bad schema")})
    service = make_service(factory, provider)

    first = service.run(limit=None)
    assert first.failed == 1
    assert ConversationUnderstandingFailureRepository(db_session).count() == 1

    second = service.run(limit=None)
    assert second.analyzed == 0
    assert second.failed == 0
    assert second.skipped_terminal_failures == 1
    assert len(provider.prompts) == 1


def test_retryable_failure_remains_pending_for_later_normal_rerun(
    factory: Any, db_session: Session
) -> None:
    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    transient = RetryableLLMExtractionError(
        "quota exhausted",
        category=LLMFailureCategory.TRANSIENT_API,
        attempts=5,
    )
    provider = FakeProvider({"marker-alpha": transient})

    first = make_service(factory, provider).run(limit=None)
    assert first.failed == 1
    assert first.retryable_failed == 1
    assert ConversationUnderstandingFailureRepository(db_session).count() == 0
    assert ConversationAnalysisRepository(db_session).count() == 0

    recovery = FakeProvider({"marker-alpha": analysis_for(["leak"])})
    second = make_service(factory, recovery).run(limit=None)
    assert second.analyzed == 1
    assert ConversationAnalysisRepository(db_session).count() == 1


def test_retry_failures_mode_reprocesses_and_clears_terminal_failure(
    factory: Any, db_session: Session
) -> None:
    conv_id = seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    provider = FakeProvider({"marker-alpha": PermanentLLMExtractionError("bad schema")})
    make_service(factory, provider).run(limit=None)
    assert ConversationUnderstandingFailureRepository(db_session).get(conv_id) is not None

    recovery = FakeProvider({"marker-alpha": analysis_for(["leak"])})
    result = make_service(factory, recovery).run(limit=None, retry_failures=True)

    assert result.analyzed == 1
    assert ConversationAnalysisRepository(db_session).count() == 1
    assert ConversationUnderstandingFailureRepository(db_session).get(conv_id) is None


def test_limit_caps_work_and_defers_catalog(factory: Any, db_session: Session) -> None:
    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    seed_conversation(db_session, "conv_b", 1, "marker-beta")
    provider = FakeProvider(
        {"marker-alpha": analysis_for(["leak"]), "marker-beta": analysis_for(["noise"])}
    )
    service = make_service(factory, provider)

    first = service.run(limit=1)
    assert first.analyzed == 1
    # Day 1 is not fully covered — the baseline catalog must not be built yet.
    assert IssueCatalogRepository(db_session).count() == 0

    second = service.run(limit=1)  # resumable: analyzes the remaining conversation
    assert second.analyzed == 1
    assert ConversationAnalysisRepository(db_session).count() == 2
    assert IssueCatalogRepository(db_session).count() == 2


def test_extraction_failure_recorded_and_skipped(factory: Any, db_session: Session) -> None:
    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    seed_conversation(db_session, "conv_b", 1, "marker-beta")
    provider = FakeProvider(
        {
            "marker-alpha": PermanentLLMExtractionError(
                "no valid response after 3 attempts",
                category=LLMFailureCategory.VALIDATION,
                attempts=3,
            ),
            "marker-beta": analysis_for(["noise"]),
        }
    )
    result = make_service(factory, provider).run(limit=None)
    assert result.analyzed == 1
    assert result.failed == 1
    assert result.permanent_failed == 1
    # Nothing persisted for the failed conversation.
    assert ConversationAnalysisRepository(db_session).count() == 1
    # A failure means Day 1 is not fully analyzed — no baseline catalog yet.
    assert IssueCatalogRepository(db_session).count() == 0
    assert "1 failed" in result.summary()


def test_progress_reports_total_completed_current_and_failures(
    factory: Any, db_session: Session
) -> None:
    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    seed_conversation(db_session, "conv_b", 1, "marker-beta")
    provider = FakeProvider(
        {
            "marker-alpha": analysis_for(["leak"]),
            "marker-beta": PermanentLLMExtractionError(
                "no valid response after 3 attempts"
            ),
        }
    )
    updates: list[Any] = []

    make_service(factory, provider).run(limit=None, progress=updates.append)

    structured = [u for u in updates if hasattr(u, "completed_work")]
    assert structured
    final = structured[-1]
    assert final.stage_key == "understand"
    assert final.total_work == 2
    assert final.completed_work == 2
    assert final.succeeded_work == 1
    assert final.remaining_work == 0
    assert final.percentage == 100
    assert final.current_item in {"conv_a", "conv_b"}
    assert final.failure_count == 1
