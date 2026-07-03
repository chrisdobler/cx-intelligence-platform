"""Integration tests for the repository layer (require local Postgres)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from cxintel.models import PipelineRun
from cxintel.repositories import (
    ConversationRepository,
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
