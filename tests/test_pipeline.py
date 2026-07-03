"""Unit tests for the pipeline orchestration layer (no database required)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from cxintel.pipeline import orchestrator
from cxintel.pipeline.jobs import JobBusyError, JobState, JobTracker
from cxintel.pipeline.orchestrator import (
    PrerequisitesUnmetError,
    run_remaining,
    run_stage,
    stage_statuses,
)
from cxintel.pipeline.progress import ProgressReporter, ProgressSnapshot
from cxintel.pipeline.stages import (
    PipelineStage,
    Prerequisite,
    StageKind,
    StageNotRunnableError,
)


class FakeStage(PipelineStage):
    """Configurable stage for orchestrator tests; records run() calls."""

    def __init__(
        self,
        key: str,
        *,
        complete: bool = False,
        implemented: bool = True,
        prereqs_met: bool = True,
        kind: StageKind = StageKind.BATCH,
        fail: bool = False,
    ) -> None:
        self.key = key
        self.label = key.title()
        self.description = f"{key} stage"
        self.outputs = (f"{key} output",)
        self.kind = kind
        self.implemented = implemented
        self.planned_phase = None if implemented else "Phase 9"
        self._complete = complete
        self._prereqs_met = prereqs_met
        self._fail = fail
        self.run_calls = 0

    def is_complete(self, session: Session | None) -> bool:
        return self._complete

    def prerequisites(self, session: Session | None) -> list[Prerequisite]:
        return [Prerequisite(label="ready", met=self._prereqs_met, detail=None)]

    def run(self, session_factory: Any, progress: Any) -> str:
        if not self.implemented or self.kind is StageKind.INTERACTIVE:
            raise StageNotRunnableError(f"{self.key} is not runnable")
        self.run_calls += 1
        if self._fail:
            raise RuntimeError(f"{self.key} exploded")
        self._complete = True
        return f"{self.key} done"


@pytest.fixture(autouse=True)
def _no_database(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep these unit tests off any real database.

    Run recording and last-run lookups degrade gracefully when the DB is
    unreachable, so pointing at a dead port keeps them pure unit tests (and
    keeps FakeStage runs out of the dev database's audit trail).
    """
    from cxintel.config import get_settings
    from cxintel.db import get_engine, get_session_factory

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://cx:cx@localhost:1/cx")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def use_stages(monkeypatch: pytest.MonkeyPatch, *stages: FakeStage) -> None:
    monkeypatch.setattr(orchestrator, "STAGES", tuple(stages))


# --- stage_statuses ---------------------------------------------------------


def test_real_registry_has_five_stages_in_order() -> None:
    statuses = stage_statuses()
    assert [s.key for s in statuses] == [
        "ingest",
        "understand",
        "anomaly",
        "knowledge_base",
        "resolution_assistant",
    ]
    by_key = {s.key: s for s in statuses}
    assert by_key["ingest"].implemented is True
    assert by_key["understand"].implemented is False
    assert by_key["understand"].planned_phase == "Phase 3"
    assert by_key["resolution_assistant"].kind == StageKind.INTERACTIVE
    # Every stage carries card data.
    for s in statuses:
        assert s.description
        assert s.prerequisites
        assert s.outputs


def test_statuses_report_runnable(monkeypatch: pytest.MonkeyPatch) -> None:
    runnable = FakeStage("a")
    blocked = FakeStage("b", prereqs_met=False)
    unimplemented = FakeStage("c", implemented=False)
    interactive = FakeStage("d", kind=StageKind.INTERACTIVE)
    use_stages(monkeypatch, runnable, blocked, unimplemented, interactive)

    by_key = {s.key: s for s in stage_statuses()}
    assert by_key["a"].runnable is True
    assert by_key["b"].runnable is False
    assert by_key["c"].runnable is False
    assert by_key["d"].runnable is False


# --- run_stage --------------------------------------------------------------


def test_run_stage_runs_and_returns_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    stage = FakeStage("a")
    use_stages(monkeypatch, stage)
    summary = run_stage("a")
    assert summary == "a done"
    assert stage.run_calls == 1
    # Run recording is DB-backed (see test_pipeline_runs.py); with the DB
    # unreachable there is no last_run, and the run itself still succeeds.
    assert stage_statuses()[0].last_run is None


def test_run_stage_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        run_stage("nope")


def test_run_stage_not_runnable(monkeypatch: pytest.MonkeyPatch) -> None:
    use_stages(monkeypatch, FakeStage("a", implemented=False))
    with pytest.raises(StageNotRunnableError):
        run_stage("a")


def test_run_stage_interactive_not_runnable(monkeypatch: pytest.MonkeyPatch) -> None:
    use_stages(monkeypatch, FakeStage("a", kind=StageKind.INTERACTIVE))
    with pytest.raises(StageNotRunnableError):
        run_stage("a")


def test_run_stage_prereqs_unmet(monkeypatch: pytest.MonkeyPatch) -> None:
    use_stages(monkeypatch, FakeStage("a", prereqs_met=False))
    with pytest.raises(PrerequisitesUnmetError):
        run_stage("a")


def test_run_stage_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    use_stages(monkeypatch, FakeStage("a", fail=True))
    with pytest.raises(RuntimeError):
        run_stage("a")


# --- run_remaining ----------------------------------------------------------


def test_run_remaining_skips_complete_and_runs_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = FakeStage("a", complete=True)
    first = FakeStage("b")
    second = FakeStage("c")
    use_stages(monkeypatch, done, first, second)

    summary = run_remaining()
    assert done.run_calls == 0
    assert first.run_calls == 1
    assert second.run_calls == 1
    assert "b" in summary.lower() and "c" in summary.lower()


def test_run_remaining_stops_cleanly_at_unimplemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = FakeStage("a")
    stub = FakeStage("b", implemented=False)
    never = FakeStage("c")
    use_stages(monkeypatch, first, stub, never)

    summary = run_remaining()
    assert first.run_calls == 1
    assert never.run_calls == 0
    assert "not yet implemented" in summary.lower()


def test_run_remaining_stops_at_unmet_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = FakeStage("a", prereqs_met=False)
    never = FakeStage("b")
    use_stages(monkeypatch, blocked, never)

    summary = run_remaining()
    assert blocked.run_calls == 0
    assert never.run_calls == 0
    assert "stopped" in summary.lower()


def test_run_remaining_skips_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = FakeStage("a", kind=StageKind.INTERACTIVE)
    batch = FakeStage("b")
    use_stages(monkeypatch, chat, batch)

    run_remaining()
    assert chat.run_calls == 0
    assert batch.run_calls == 1


def test_run_remaining_nothing_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    use_stages(monkeypatch, FakeStage("a", complete=True))
    summary = run_remaining()
    assert "complete" in summary.lower() or "nothing" in summary.lower()


def test_run_remaining_propagates_stage_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    boom = FakeStage("a", fail=True)
    never = FakeStage("b")
    use_stages(monkeypatch, boom, never)
    with pytest.raises(RuntimeError):
        run_remaining()
    assert never.run_calls == 0


# --- JobTracker -------------------------------------------------------------


def make_inline_tracker(monkeypatch: pytest.MonkeyPatch) -> JobTracker:
    tracker = JobTracker()
    monkeypatch.setattr(tracker, "_spawn", lambda fn: fn())
    return tracker


def test_job_tracker_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = make_inline_tracker(monkeypatch)
    tracker.start("ingest", lambda progress: "all good")
    job = tracker.current()
    assert job is not None
    assert job.state == JobState.SUCCEEDED
    assert job.message == "all good"
    assert job.target == "ingest"
    assert job.started_at is not None and job.finished_at is not None


def test_job_tracker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = make_inline_tracker(monkeypatch)

    def boom(progress: Any) -> str:
        raise RuntimeError("kaput")

    tracker.start("ingest", boom)
    job = tracker.current()
    assert job is not None
    assert job.state == JobState.FAILED
    assert job.error is not None and "kaput" in job.error


def test_job_tracker_progress_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = make_inline_tracker(monkeypatch)
    seen: list[str] = []

    def work(progress: Any) -> str:
        progress("halfway")
        seen.append(tracker.current().progress)  # type: ignore[union-attr]
        return "done"

    tracker.start("pipeline", work)
    assert seen == ["halfway"]


def test_job_tracker_structured_progress_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = make_inline_tracker(monkeypatch)
    seen: list[ProgressSnapshot] = []

    def work(progress: Any) -> str:
        reporter = ProgressReporter(
            stage_key="ingest",
            stage_label="Data Ingestion",
            progress=progress,
            total_work=4,
            message="Importing…",
        )
        reporter.advance(current_item="conv_1", count=2, message="Halfway.")
        job = tracker.current()
        assert job is not None and job.progress_detail is not None
        seen.append(job.progress_detail)
        return "done"

    tracker.start("pipeline", work)
    assert seen[0].stage_key == "ingest"
    assert seen[0].completed_work == 2
    assert seen[0].total_work == 4
    assert seen[0].percentage == 50
    assert seen[0].current_item == "conv_1"
    assert seen[0].throughput_conversations_per_second >= 0


def test_job_tracker_failure_preserves_structured_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = make_inline_tracker(monkeypatch)

    def boom(progress: Any) -> str:
        progress(
            ProgressSnapshot(
                stage_key="understand",
                stage_label="Conversation Understanding",
                total_work=2,
                completed_work=1,
                message="working",
            )
        )
        raise RuntimeError("kaput")

    tracker.start("understand", boom)
    job = tracker.current()
    assert job is not None and job.progress_detail is not None
    assert job.state == JobState.FAILED
    assert job.progress_detail.failure_count == 1
    assert job.progress_detail.message == "kaput"


def test_job_tracker_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = JobTracker()
    # Spawn that never runs — job stays RUNNING.
    monkeypatch.setattr(tracker, "_spawn", lambda fn: None)
    tracker.start("ingest", lambda progress: "never finishes")
    with pytest.raises(JobBusyError):
        tracker.start("ingest", lambda progress: "second")
