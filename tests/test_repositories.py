"""Integration tests for the repository layer (require local Postgres)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from cxintel.models import (
    Conversation,
    ConversationIssue,
    IssueCatalogEntry,
    LLMCallObservation,
    PipelineRun,
)
from cxintel.repositories import (
    AnomalyRepository,
    ConversationIssueRepository,
    ConversationRepository,
    ConversationUnderstandingFailureRepository,
    IssueCatalogRepository,
    LLMCallObservationRepository,
    MessageRepository,
    PipelineRunRepository,
)


def conversation_row(external_id: str, status: str = "open", day: int = 1) -> dict[str, Any]:
    ts = datetime(2026, 2, 24, 12, 0, tzinfo=UTC)
    return {
        "id": uuid.uuid5(uuid.NAMESPACE_URL, external_id),
        "external_id": external_id,
        "customer_id": "cust_x",
        "status": status,
        "priority": "medium",
        "category": "hardware",
        "issue_type": "leak",
        "product": "Pod 4",
        "day": day,
        "started_at": ts,
        "ended_at": ts,
        "created_at": ts,
        "updated_at": ts,
        "resolution_type": None,
        "resolution_notes": None,
        "resolved_at": None,
        "source_metadata": None,
    }


def test_bulk_insert_is_idempotent(db_session: Session) -> None:
    repo = ConversationRepository(db_session)
    rows = [conversation_row("conv_a"), conversation_row("conv_b")]
    assert repo.bulk_insert_ignore_conflicts(rows) == 2
    db_session.commit()
    # Rerun: same rows conflict on external_id/pk and are skipped.
    assert repo.bulk_insert_ignore_conflicts(rows) == 0
    db_session.commit()
    assert repo.count() == 2


def test_count_by_status_groups(db_session: Session) -> None:
    repo = ConversationRepository(db_session)
    repo.bulk_insert_ignore_conflicts(
        [
            conversation_row("conv_a", status="resolved"),
            conversation_row("conv_b", status="resolved"),
            conversation_row("conv_c", status="escalated"),
        ]
    )
    db_session.commit()
    assert repo.count_by_status() == {"resolved": 2, "escalated": 1}


def test_date_range(db_session: Session) -> None:
    repo = ConversationRepository(db_session)
    assert repo.date_range() is None  # empty table

    early = conversation_row("conv_a")
    early["started_at"] = datetime(2026, 2, 24, 0, 0, tzinfo=UTC)
    late = conversation_row("conv_b")
    late["ended_at"] = datetime(2026, 3, 5, 19, 24, tzinfo=UTC)
    repo.bulk_insert_ignore_conflicts([early, late])
    db_session.commit()

    date_range = repo.date_range()
    assert date_range is not None
    assert date_range[0] == datetime(2026, 2, 24, 0, 0, tzinfo=UTC)
    assert date_range[1] == datetime(2026, 3, 5, 19, 24, tzinfo=UTC)


def test_get_by_external_id(db_session: Session) -> None:
    repo = ConversationRepository(db_session)
    repo.bulk_insert_ignore_conflicts([conversation_row("conv_a")])
    db_session.commit()
    found = repo.get_by_external_id("conv_a")
    assert found is not None
    assert found.external_id == "conv_a"
    assert repo.get_by_external_id("conv_missing") is None


def test_message_repository_insert_and_count(db_session: Session) -> None:
    conv_repo = ConversationRepository(db_session)
    conv_repo.bulk_insert_ignore_conflicts([conversation_row("conv_a")])
    msg_repo = MessageRepository(db_session)
    conv_id = uuid.uuid5(uuid.NAMESPACE_URL, "conv_a")
    rows = [
        {
            "id": uuid.uuid5(uuid.NAMESPACE_URL, "conv_a_msg001"),
            "external_id": "conv_a_msg001",
            "conversation_id": conv_id,
            "role": "customer",
            "body": "hello",
            "created_at": datetime(2026, 2, 24, 12, 0, tzinfo=UTC),
        }
    ]
    assert msg_repo.bulk_insert_ignore_conflicts(rows) == 1
    assert msg_repo.bulk_insert_ignore_conflicts(rows) == 0
    db_session.commit()
    assert msg_repo.count() == 1


def pipeline_run(
    stage_key: str = "ingest",
    *,
    status: str = "succeeded",
    minute: int = 0,
    trigger: str = "api",
) -> PipelineRun:
    started = datetime(2026, 7, 3, 9, minute, tzinfo=UTC)
    finished = status != "running"
    return PipelineRun(
        id=uuid.uuid4(),
        stage_key=stage_key,
        status=status,
        trigger=trigger,
        started_at=started,
        finished_at=started if finished else None,
        duration_seconds=1.5 if finished else None,
        summary=f"{stage_key} ok" if status == "succeeded" else None,
        error="boom" if status == "failed" else None,
    )


def test_pipeline_run_add_get_and_update(db_session: Session) -> None:
    repo = PipelineRunRepository(db_session)
    run = pipeline_run(status="running")
    repo.add(run)
    db_session.commit()

    found = repo.get(run.id)
    assert found is not None
    assert found.status == "running"
    assert found.finished_at is None

    found.status = "succeeded"
    found.finished_at = datetime(2026, 7, 3, 9, 1, tzinfo=UTC)
    found.duration_seconds = 60.0
    found.summary = "done"
    db_session.commit()
    assert repo.get(run.id).status == "succeeded"  # type: ignore[union-attr]


def test_latest_finished_per_stage_prefers_newest_and_skips_running(
    db_session: Session,
) -> None:
    repo = PipelineRunRepository(db_session)
    old = pipeline_run("ingest", minute=0)
    newer = pipeline_run("ingest", status="failed", minute=5)
    running = pipeline_run("ingest", status="running", minute=10)
    other = pipeline_run("understand", minute=2)
    for run in (old, newer, running, other):
        repo.add(run)
    db_session.commit()

    latest = repo.latest_finished_per_stage()
    assert set(latest) == {"ingest", "understand"}
    # The newest *finished* run wins; the still-running row is ignored.
    assert latest["ingest"].id == newer.id
    assert latest["understand"].id == other.id


def test_recent_orders_newest_first_and_limits(db_session: Session) -> None:
    repo = PipelineRunRepository(db_session)
    runs = [pipeline_run("ingest", minute=m) for m in range(5)]
    for run in runs:
        repo.add(run)
    db_session.commit()

    recent = repo.recent(limit=3)
    assert len(recent) == 3
    assert [r.id for r in recent] == [runs[4].id, runs[3].id, runs[2].id]
    assert repo.recent(limit=50)[-1].id == runs[0].id


def llm_observation(
    conversation_id: uuid.UUID,
    *,
    pipeline_run_id: uuid.UUID | None,
    total_seconds: float,
    llm_seconds: float,
    load_seconds: float = 0.01,
    prompt_seconds: float = 0.02,
    persist_seconds: float = 0.03,
    retry_count: int = 0,
) -> LLMCallObservation:
    ts = datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    return LLMCallObservation(
        id=uuid.uuid4(),
        pipeline_run_id=pipeline_run_id,
        conversation_id=conversation_id,
        day=1,
        model="gemini-2.5-flash",
        prompt_version="v1",
        status="succeeded",
        total_seconds=total_seconds,
        load_seconds=load_seconds,
        prompt_seconds=prompt_seconds,
        llm_seconds=llm_seconds,
        persist_seconds=persist_seconds,
        message_count=3,
        prompt_characters=1200,
        issue_count=1,
        retry_count=retry_count,
        started_at=ts,
        finished_at=ts,
        error=None,
    )


def test_llm_observation_slowest_sorts_and_filters_by_run(db_session: Session) -> None:
    conv_a = seeded_conversation(db_session, "conv_a")
    conv_b = seeded_conversation(db_session, "conv_b")
    run_a = pipeline_run("understand", minute=0)
    run_b = pipeline_run("understand", minute=1)
    run_repo = PipelineRunRepository(db_session)
    run_repo.add(run_a)
    run_repo.add(run_b)
    db_session.flush()

    repo = LLMCallObservationRepository(db_session)
    repo.add(
        llm_observation(
            conv_a, pipeline_run_id=run_a.id, total_seconds=3.0, llm_seconds=2.5
        )
    )
    repo.add(
        llm_observation(
            conv_b, pipeline_run_id=run_b.id, total_seconds=8.0, llm_seconds=1.0
        )
    )
    repo.add(
        llm_observation(
            conv_a,
            pipeline_run_id=run_a.id,
            total_seconds=4.0,
            llm_seconds=3.5,
            retry_count=2,
        )
    )
    db_session.commit()

    assert repo.count() == 3
    assert [row.llm_seconds for row in repo.slowest(sort="llm_seconds")] == [3.5, 2.5, 1.0]
    assert [row.total_seconds for row in repo.slowest(sort="total_seconds", limit=2)] == [
        8.0,
        4.0,
    ]
    assert {row.pipeline_run_id for row in repo.slowest(pipeline_run_id=run_a.id)} == {run_a.id}


def make_issue(
    conversation_id: uuid.UUID,
    canonical_name: str = "base water leak",
    *,
    matched: bool = True,
) -> ConversationIssue:
    return ConversationIssue(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        canonical_name=canonical_name,
        customer_description="water pooling under the pod",
        severity="high",
        confidence=0.95,
        customer_impact="high",
        product="Pod 5",
        symptoms=["water pooling"],
        catalog_matched=matched,
        catalog_confidence=0.9,
        resolution_status="resolved",
        resolution_summary="replaced",
        created_at=datetime(2026, 7, 3, tzinfo=UTC),
    )


def seeded_conversation(db_session: Session, external_id: str, day: int = 1) -> uuid.UUID:
    ConversationRepository(db_session).bulk_insert_ignore_conflicts(
        [conversation_row(external_id, day=day)]
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, external_id)


def test_understanding_failure_repository_upserts_and_clears(db_session: Session) -> None:
    conv_id = seeded_conversation(db_session, "conv_a")
    repo = ConversationUnderstandingFailureRepository(db_session)
    first = datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    second = datetime(2026, 7, 3, 10, 5, tzinfo=UTC)

    repo.upsert(
        conversation_id=conv_id,
        pipeline_run_id=None,
        day=1,
        model="gemini-2.5-flash",
        prompt_version="v1",
        status="terminal",
        failure_category="validation",
        error="bad shape",
        retry_count=2,
        failed_at=first,
    )
    db_session.commit()
    assert repo.count() == 1

    repo.upsert(
        conversation_id=conv_id,
        pipeline_run_id=None,
        day=1,
        model="gemini-2.5-flash",
        prompt_version="v1",
        status="terminal",
        failure_category="permanent_api",
        error="bad request",
        retry_count=0,
        failed_at=second,
    )
    db_session.commit()
    failure = repo.get(conv_id)
    assert failure is not None
    assert failure.first_failed_at == first
    assert failure.last_failed_at == second
    assert failure.failure_category == "permanent_api"

    repo.clear(conv_id)
    db_session.commit()
    assert repo.count() == 0


def test_issue_replace_for_conversation_regenerates(db_session: Session) -> None:
    conv_id = seeded_conversation(db_session, "conv_a")
    repo = ConversationIssueRepository(db_session)
    repo.replace_for_conversation(conv_id, [make_issue(conv_id, "old issue")])
    db_session.commit()
    assert repo.count() == 1

    repo.replace_for_conversation(
        conv_id, [make_issue(conv_id, "new issue"), make_issue(conv_id, "second issue")]
    )
    db_session.commit()
    names = {i.canonical_name for i in db_session.query(ConversationIssue).all()}
    assert names == {"new issue", "second issue"}


def test_issue_canonical_names_for_day(db_session: Session) -> None:
    conv1 = seeded_conversation(db_session, "conv_a", day=1)
    conv2 = seeded_conversation(db_session, "conv_b", day=2)
    repo = ConversationIssueRepository(db_session)
    repo.replace_for_conversation(conv1, [make_issue(conv1, "leak"), make_issue(conv1, "noise")])
    repo.replace_for_conversation(conv2, [make_issue(conv2, "day2 only")])
    db_session.commit()
    assert repo.canonical_names_for_day(1) == ["leak", "noise"]


def test_issue_day_aggregation(db_session: Session) -> None:
    conv1 = seeded_conversation(db_session, "conv_a", day=1)
    conv2 = seeded_conversation(db_session, "conv_b", day=1)
    conv3 = seeded_conversation(db_session, "conv_c", day=2)
    repo = ConversationIssueRepository(db_session)
    repo.replace_for_conversation(conv1, [make_issue(conv1, "leak")])
    repo.replace_for_conversation(conv2, [make_issue(conv2, "leak"), make_issue(conv2, "noise")])
    repo.replace_for_conversation(conv3, [make_issue(conv3, "leak")])  # day 2 — excluded
    db_session.commit()

    agg = repo.aggregate_for_day(1)
    assert {(a.canonical_name, a.example_count) for a in agg} == {("leak", 2), ("noise", 1)}
    leak = next(a for a in agg if a.canonical_name == "leak")
    assert "water pooling under the pod" in leak.examples


def test_issue_day_aggregation_orders_examples_deterministically(
    db_session: Session,
) -> None:
    late = seeded_conversation(db_session, "conv_late", day=1)
    early = seeded_conversation(db_session, "conv_early", day=1)
    late_conversation = db_session.get(Conversation, late)
    early_conversation = db_session.get(Conversation, early)
    assert late_conversation is not None
    assert early_conversation is not None
    late_conversation.started_at = datetime(2026, 2, 24, 12, 5, tzinfo=UTC)
    early_conversation.started_at = datetime(2026, 2, 24, 12, 0, tzinfo=UTC)

    late_issue = make_issue(late, "leak")
    late_issue.customer_description = "late example"
    early_issue = make_issue(early, "leak")
    early_issue.customer_description = "early example"
    repo = ConversationIssueRepository(db_session)
    repo.replace_for_conversation(late, [late_issue])
    repo.replace_for_conversation(early, [early_issue])
    db_session.commit()

    leak = repo.aggregate_for_day(1)[0]
    assert leak.examples == ["early example", "late example"]


def test_issue_unmatched_count(db_session: Session) -> None:
    conv = seeded_conversation(db_session, "conv_a", day=2)
    repo = ConversationIssueRepository(db_session)
    repo.replace_for_conversation(
        conv, [make_issue(conv, "known", matched=True), make_issue(conv, "novel", matched=False)]
    )
    db_session.commit()
    assert repo.unmatched_count() == 1


def test_day_issue_stats_aggregates_counts_severity_and_resolution(
    db_session: Session,
) -> None:
    conv1 = seeded_conversation(db_session, "conv_a", day=2)
    conv2 = seeded_conversation(db_session, "conv_b", day=2)
    conv3 = seeded_conversation(db_session, "conv_c", day=1)  # other day — excluded
    repo = ConversationIssueRepository(db_session)

    leak_high = make_issue(conv1, "leak")  # severity high, resolved, matched
    leak_low = make_issue(conv2, "leak", matched=False)
    leak_low.severity = "low"
    leak_low.resolution_status = "unresolved"
    repo.replace_for_conversation(conv1, [leak_high])
    repo.replace_for_conversation(conv2, [leak_low])
    repo.replace_for_conversation(conv3, [make_issue(conv3, "leak")])
    db_session.commit()

    stats = {s.canonical_name: s for s in repo.day_issue_stats(2)}
    assert set(stats) == {"leak"}
    leak = stats["leak"]
    assert leak.count == 2
    assert leak.high_severity_count == 1  # 'high' counts; 'critical' would too
    assert leak.resolved_count == 1
    assert leak.unmatched_count == 1


def test_anomaly_replace_all_and_for_days(db_session: Session) -> None:
    from cxintel.models import Anomaly

    repo = AnomalyRepository(db_session)

    def anomaly(issue: str, day: int, severity: str = "high") -> Anomaly:
        return Anomaly(
            id=uuid.uuid4(),
            day=day,
            observation_date=None,
            baseline_date=None,
            issue=issue,
            severity=severity,
            delta=100.0,
            description=f"{issue} spiked",
            slack_message=f"alert: {issue}",
            signals=["volume_spike"],
            metrics={"baseline_count": 10, "current_count": 20},
            recommended_action="investigate",
            created_at=datetime(2026, 7, 3, tzinfo=UTC),
        )

    repo.replace_all([anomaly("leak", 2), anomaly("noise", 3, "critical")])
    db_session.commit()
    assert repo.count() == 2

    rows = repo.for_days([2, 3])
    assert [(a.issue, a.day) for a in rows] == [("leak", 2), ("noise", 3)]
    assert rows[0].signals == ["volume_spike"]
    assert rows[0].metrics["current_count"] == 20

    # Derived data: replace_all regenerates without duplicates.
    repo.replace_all([anomaly("leak", 2)])
    db_session.commit()
    assert repo.count() == 1


def test_issue_catalog_replace_all_and_all(db_session: Session) -> None:
    repo = IssueCatalogRepository(db_session)
    assert repo.all() == []
    entries = [
        IssueCatalogEntry(
            canonical_name="leak",
            description="water pooling under the pod",
            first_seen_day=1,
            example_count=12,
            representative_examples=["water pooling under the pod"],
            created_at=datetime(2026, 7, 3, tzinfo=UTC),
        )
    ]
    repo.replace_all(entries)
    db_session.commit()
    assert [e.canonical_name for e in repo.all()] == ["leak"]
    assert repo.count() == 1

    # Regenerable: replace_all wipes and rebuilds.
    repo.replace_all([])
    db_session.commit()
    assert repo.count() == 0
