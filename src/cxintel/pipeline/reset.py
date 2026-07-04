"""Developer reset actions for regenerable pipeline artifacts."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from ..db import get_session_factory
from ..models import PipelineRun
from ..repositories import PipelineRunRepository

RESET_DERIVED_STAGE_KEY = "reset_derived"
RESET_DERIVED_SUMMARY = (
    "Reset derived AI artifacts: conversation analyses, conversation issues, "
    "issue catalog, anomalies cleared."
)

_TRUNCATE_DERIVED_SQL = text(
    """
    TRUNCATE TABLE
        conversation_issues,
        conversation_analyses,
        issue_catalog,
        anomalies
    RESTART IDENTITY CASCADE
    """
)


def _record_finish(
    run_id: uuid.UUID,
    trigger: str,
    started_at: datetime,
    duration_seconds: float,
    *,
    summary: str | None = None,
    error: str | None = None,
) -> None:
    """Finalize the reset audit row, inserting it if the start row vanished."""
    with get_session_factory()() as session:
        repo = PipelineRunRepository(session)
        run = repo.get(run_id)
        if run is None:
            run = PipelineRun(
                id=run_id,
                stage_key=RESET_DERIVED_STAGE_KEY,
                status="running",
                trigger=trigger,
                started_at=started_at,
            )
            repo.add(run)
        run.status = "succeeded" if error is None else "failed"
        run.finished_at = datetime.now(tz=UTC)
        run.duration_seconds = duration_seconds
        run.summary = summary
        run.error = error
        session.commit()


def reset_derived_data(*, trigger: str = "api") -> str:
    """Clear regenerable AI artifacts and record a pipeline audit entry.

    Imported conversations and messages are source data and are intentionally
    not touched. Pipeline run history is also preserved as audit evidence.
    """
    run_id = uuid.uuid4()
    started_at = datetime.now(tz=UTC)
    started = time.monotonic()

    with get_session_factory()() as session:
        PipelineRunRepository(session).add(
            PipelineRun(
                id=run_id,
                stage_key=RESET_DERIVED_STAGE_KEY,
                status="running",
                trigger=trigger,
                started_at=started_at,
            )
        )
        session.commit()

    try:
        with get_session_factory()() as session:
            session.execute(_TRUNCATE_DERIVED_SQL)
            session.commit()
    except Exception as exc:
        _record_finish(
            run_id,
            trigger,
            started_at,
            time.monotonic() - started,
            error=str(exc),
        )
        raise

    _record_finish(
        run_id,
        trigger,
        started_at,
        time.monotonic() - started,
        summary=RESET_DERIVED_SUMMARY,
    )
    return RESET_DERIVED_SUMMARY
