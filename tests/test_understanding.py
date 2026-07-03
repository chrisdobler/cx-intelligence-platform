"""DB-backed tests for the Conversation Understanding service (fake provider)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from cxintel.llm import RetryCallback
from cxintel.models import Conversation, ConversationIssue, Message
from cxintel.repositories import (
    ConversationAnalysisRepository,
    ConversationIssueRepository,
    IssueCatalogRepository,
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

    def extract(
        self, prompt: str, schema: type[T], on_retry: RetryCallback | None = None
    ) -> T:
        self.prompts.append(prompt)
        for marker, result in self.by_marker.items():
            if marker in prompt:
                if isinstance(result, Exception):
                    raise result
                return cast(T, result)
        raise AssertionError(f"no canned analysis matches prompt: {prompt[:200]}")


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


def make_service(factory: Any, provider: FakeProvider) -> UnderstandingService:
    return UnderstandingService(factory, provider, concurrency=1)


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
            "marker-day1": analysis_for(["base water leak"]),
            "marker-day2": analysis_for(["totally novel problem"], matched=False),
        }
    )
    result = make_service(factory, provider).run(limit=None)
    assert result.analyzed == 2

    catalog = IssueCatalogRepository(db_session).all()
    # Catalog derives from Day 1 only — the novel Day-2 issue is NOT added.
    assert [e.canonical_name for e in catalog] == ["base water leak"]
    entry = catalog[0]
    assert entry.first_seen_day == 1
    assert entry.example_count == 1
    assert entry.representative_examples == ["customer says base water leak"]

    # The Day-2 prompt received the Day-1 catalog for normalization.
    day2_prompt = next(p for p in provider.prompts if "marker-day2" in p)
    assert "base water leak" in day2_prompt

    # The novel Day-2 issue surfaces as a candidate novel issue.
    assert ConversationIssueRepository(db_session).unmatched_count() == 1


def test_rerun_skips_already_analyzed(factory: Any, db_session: Session) -> None:
    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    provider = FakeProvider({"marker-alpha": analysis_for(["leak"])})
    service = make_service(factory, provider)
    assert service.run(limit=None).analyzed == 1

    rerun = service.run(limit=None)
    assert rerun.analyzed == 0
    assert rerun.skipped_existing == 1
    assert len(provider.prompts) == 1  # provider not called again


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
    from cxintel.llm import LLMExtractionError

    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    seed_conversation(db_session, "conv_b", 1, "marker-beta")
    provider = FakeProvider(
        {
            "marker-alpha": LLMExtractionError("no valid response after 3 attempts"),
            "marker-beta": analysis_for(["noise"]),
        }
    )
    result = make_service(factory, provider).run(limit=None)
    assert result.analyzed == 1
    assert result.failed == 1
    # Nothing persisted for the failed conversation.
    assert ConversationAnalysisRepository(db_session).count() == 1
    # A failure means Day 1 is not fully analyzed — no baseline catalog yet.
    assert IssueCatalogRepository(db_session).count() == 0
    assert "1 failed" in result.summary()


def test_progress_reports_total_completed_current_and_failures(
    factory: Any, db_session: Session
) -> None:
    from cxintel.llm import LLMExtractionError

    seed_conversation(db_session, "conv_a", 1, "marker-alpha")
    seed_conversation(db_session, "conv_b", 1, "marker-beta")
    provider = FakeProvider(
        {
            "marker-alpha": analysis_for(["leak"]),
            "marker-beta": LLMExtractionError("no valid response after 3 attempts"),
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
    assert final.percentage == 100
    assert final.current_item in {"conv_a", "conv_b"}
    assert final.failure_count == 1
