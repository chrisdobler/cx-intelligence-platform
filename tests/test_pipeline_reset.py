"""DB-backed tests for resetting regenerable pipeline artifacts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from cxintel.models import (
    Anomaly,
    ConversationAnalysis,
    IssueCatalogEntry,
    LLMCallObservation,
)
from cxintel.pipeline.reset import (
    RESET_DERIVED_STAGE_KEY,
    RESET_DERIVED_SUMMARY,
    reset_derived_data,
)
from cxintel.repositories import (
    AnomalyRepository,
    ConversationAnalysisRepository,
    ConversationIssueRepository,
    ConversationRepository,
    IssueCatalogRepository,
    LLMCallObservationRepository,
    MessageRepository,
    PipelineRunRepository,
)

from .test_repositories import make_issue, pipeline_run
from .test_understanding import seed_conversation


def _seed_derived_artifacts(session: Session) -> uuid.UUID:
    conv_id = seed_conversation(session, "conv_reset", 1, "reset marker")
    now = datetime(2026, 7, 3, tzinfo=UTC)
    session.merge(
        ConversationAnalysis(
            conversation_id=conv_id,
            model="fake",
            model_version="fake",
            prompt_version="1.0",
            processed_at=now,
            analysis_json={"summary": "derived"},
        )
    )
    session.add(make_issue(conv_id, "reset issue"))
    session.merge(
        IssueCatalogEntry(
            canonical_name="reset issue",
            description="reset issue",
            first_seen_day=1,
            example_count=1,
            representative_examples=["reset issue"],
            created_at=now,
        )
    )
    session.add(
        Anomaly(
            id=uuid.uuid4(),
            day=2,
            observation_date=None,
            baseline_date=None,
            issue="reset issue",
            severity="high",
            delta=100.0,
            description="reset issue spiked",
            slack_message="alert",
            signals=["volume_spike"],
            metrics={"baseline_count": 1, "current_count": 2},
            recommended_action="investigate",
            created_at=now,
        )
    )
    session.commit()
    return conv_id


def _seed_preserved_audit_data(session: Session, conv_id: uuid.UUID) -> uuid.UUID:
    run = pipeline_run("understand")
    PipelineRunRepository(session).add(run)
    session.flush()
    session.add(
        LLMCallObservation(
            id=uuid.uuid4(),
            pipeline_run_id=run.id,
            conversation_id=conv_id,
            day=1,
            model="gemini-2.5-flash",
            prompt_version="v1",
            status="succeeded",
            total_seconds=1.0,
            load_seconds=0.1,
            prompt_seconds=0.1,
            llm_seconds=0.7,
            persist_seconds=0.1,
            message_count=1,
            prompt_characters=100,
            issue_count=1,
            retry_count=0,
            started_at=datetime(2026, 7, 3, tzinfo=UTC),
            finished_at=datetime(2026, 7, 3, tzinfo=UTC),
            error=None,
        )
    )
    session.commit()
    return run.id


def _derived_counts(session: Session) -> tuple[int, int, int, int]:
    return (
        ConversationAnalysisRepository(session).count(),
        ConversationIssueRepository(session).count(),
        IssueCatalogRepository(session).count(),
        AnomalyRepository(session).count(),
    )


def test_reset_derived_data_clears_only_ai_artifacts(
    settings_on_test_db: str, db_session: Session
) -> None:
    conv_id = _seed_derived_artifacts(db_session)
    preserved_run_id = _seed_preserved_audit_data(db_session, conv_id)

    assert ConversationRepository(db_session).count() == 1
    assert MessageRepository(db_session).count() == 1
    assert _derived_counts(db_session) == (1, 1, 1, 1)
    assert LLMCallObservationRepository(db_session).count() == 1
    db_session.rollback()

    summary = reset_derived_data(trigger="api")
    db_session.expire_all()

    assert summary == RESET_DERIVED_SUMMARY
    assert ConversationRepository(db_session).count() == 1
    assert MessageRepository(db_session).count() == 1
    assert _derived_counts(db_session) == (0, 0, 0, 0)
    assert PipelineRunRepository(db_session).get(preserved_run_id) is not None
    assert LLMCallObservationRepository(db_session).count() == 1

    runs = PipelineRunRepository(db_session).recent(limit=5)
    reset_run = runs[0]
    assert reset_run.stage_key == RESET_DERIVED_STAGE_KEY
    assert reset_run.status == "succeeded"
    assert reset_run.trigger == "api"
    assert reset_run.summary == RESET_DERIVED_SUMMARY


def test_reset_derived_data_is_repeatable(settings_on_test_db: str, db_session: Session) -> None:
    _seed_derived_artifacts(db_session)
    db_session.rollback()

    reset_derived_data(trigger="api")
    reset_derived_data(trigger="api")
    db_session.expire_all()

    assert ConversationRepository(db_session).count() == 1
    assert MessageRepository(db_session).count() == 1
    assert _derived_counts(db_session) == (0, 0, 0, 0)
    reset_runs = [
        run
        for run in PipelineRunRepository(db_session).recent(limit=10)
        if run.stage_key == RESET_DERIVED_STAGE_KEY
    ]
    assert len(reset_runs) == 2


def test_reset_derived_data_records_failed_audit_row(
    settings_on_test_db: str, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text

    monkeypatch.setattr("cxintel.pipeline.reset._TRUNCATE_DERIVED_SQL", text("select * from nope"))

    with pytest.raises(SQLAlchemyError):
        reset_derived_data(trigger="api")

    db_session.expire_all()
    run = PipelineRunRepository(db_session).recent(limit=1)[0]
    assert run.stage_key == RESET_DERIVED_STAGE_KEY
    assert run.status == "failed"
    assert run.error is not None
