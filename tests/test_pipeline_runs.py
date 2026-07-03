"""DB-backed tests for durable run recording (the pipeline audit trail)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from cxintel.pipeline import orchestrator
from cxintel.pipeline.orchestrator import run_stage, stage_statuses
from cxintel.repositories import PipelineRunRepository

from .test_pipeline import FakeStage


def use_stages(monkeypatch: pytest.MonkeyPatch, *stages: FakeStage) -> None:
    monkeypatch.setattr(orchestrator, "STAGES", tuple(stages))


def recorded_runs(db_session: Any) -> list[Any]:
    db_session.expire_all()
    return PipelineRunRepository(db_session).recent(limit=50)


def test_successful_run_is_recorded(
    settings_on_test_db: str, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stages(monkeypatch, FakeStage("a"))
    run_stage("a")

    runs = recorded_runs(db_session)
    assert len(runs) == 1
    run = runs[0]
    assert run.stage_key == "a"
    assert run.status == "succeeded"
    assert run.trigger == "api"  # default
    assert run.summary == "a done"
    assert run.error is None
    assert run.finished_at is not None
    assert run.duration_seconds is not None and run.duration_seconds >= 0


def test_failed_run_is_recorded_with_error(
    settings_on_test_db: str, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stages(monkeypatch, FakeStage("a", fail=True))
    with pytest.raises(RuntimeError):
        run_stage("a")

    runs = recorded_runs(db_session)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error is not None and "exploded" in runs[0].error
    assert runs[0].summary is None


def test_cli_trigger_is_recorded(
    settings_on_test_db: str, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stages(monkeypatch, FakeStage("a"))
    run_stage("a", trigger="cli")
    assert recorded_runs(db_session)[0].trigger == "cli"


def test_running_row_exists_while_stage_executes(
    settings_on_test_db: str, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start row is written before run() — a crash would leave it as evidence."""
    observed: list[str] = []

    class ObservantStage(FakeStage):
        def run(
            self,
            session_factory: Any,
            progress: Any,
            option: str | None = None,
            run_id: uuid.UUID | None = None,
        ) -> str:
            with session_factory() as session:
                runs = PipelineRunRepository(session).recent(limit=5)
                observed.extend(r.status for r in runs if r.stage_key == "a")
            return super().run(session_factory, progress, option, run_id)

    use_stages(monkeypatch, ObservantStage("a"))
    run_stage("a")
    assert observed == ["running"]


def test_finish_row_inserted_even_when_start_insert_failed(
    settings_on_test_db: str, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh-DB case: the ingest run itself creates pipeline_runs via migrations,
    so the start insert can fail — the finished run must still be recorded."""
    monkeypatch.setattr(orchestrator, "_record_start", lambda *a, **kw: None)
    use_stages(monkeypatch, FakeStage("a"))
    run_stage("a")

    runs = recorded_runs(db_session)
    assert len(runs) == 1
    assert runs[0].status == "succeeded"


def test_last_run_survives_settings_reload(
    settings_on_test_db: str, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart proof: last_run comes from the database, not process memory."""
    from cxintel.config import get_settings

    use_stages(monkeypatch, FakeStage("a"))
    run_stage("a")

    get_settings.cache_clear()  # simulate a fresh process reading config anew
    status = stage_statuses()[0]
    assert status.last_run is not None
    assert status.last_run.ok is True
    assert status.last_run.summary == "a done"


def test_last_run_reports_failure(
    settings_on_test_db: str, db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_stages(monkeypatch, FakeStage("a", fail=True))
    with pytest.raises(RuntimeError):
        run_stage("a")
    status = stage_statuses()[0]
    assert status.last_run is not None
    assert status.last_run.ok is False
    assert "exploded" in status.last_run.summary
