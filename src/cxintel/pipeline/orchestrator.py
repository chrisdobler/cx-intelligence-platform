"""Pipeline orchestrator — the single entry point for running stages.

The CLI (``app ingest`` / ``app pipeline``), the REST API
(``POST /api/pipeline/…``), and the landing-page control center all call this
module, so stage behaviour is defined exactly once. ``STAGES`` is ordered by
dependency (the pipeline is linear, so list order is a valid topological
order); each stage still declares its own explicit prerequisites.

Last-run records are kept in memory by design: a durable run-history table is
listed as future work in ARCHITECTURE.md and is not needed for a single-node
control center.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy.orm import Session

from .stages import (
    AnomalyStage,
    IngestStage,
    KnowledgeBaseStage,
    PipelineStage,
    Prerequisite,
    ProgressCallback,
    ResolutionAssistantStage,
    StageKind,
    UnderstandStage,
)

STAGES: tuple[PipelineStage, ...] = (
    IngestStage(),
    UnderstandStage(),
    KnowledgeBaseStage(),
    AnomalyStage(),
    ResolutionAssistantStage(),
)


class PrerequisitesUnmetError(Exception):
    """Raised when a stage is asked to run before its prerequisites are met."""


class LastRun(BaseModel):
    """In-memory record of a stage's most recent execution."""

    finished_at: datetime
    duration_seconds: float
    summary: str
    ok: bool


class StageStatus(BaseModel):
    """Snapshot of one stage — everything a control-center card needs."""

    key: str
    label: str
    description: str
    kind: StageKind
    implemented: bool
    planned_phase: str | None
    complete: bool
    runnable: bool
    prerequisites: list[Prerequisite]
    outputs: list[str]
    last_run: LastRun | None
    open_url: str | None


_LAST_RUNS: dict[str, LastRun] = {}


def get_stage(key: str) -> PipelineStage:
    """Look up a stage by key; raises ``KeyError`` for unknown keys."""
    for stage in STAGES:
        if stage.key == key:
            return stage
    raise KeyError(key)


def _open_session() -> Session | None:
    """A working session, or ``None`` when the database is unreachable."""
    from ..db import get_session_factory

    try:
        session = get_session_factory()()
        session.connection()
        return session
    except Exception:
        return None


def _status(stage: PipelineStage, session: Session | None) -> StageStatus:
    prerequisites = stage.prerequisites(session)
    return StageStatus(
        key=stage.key,
        label=stage.label,
        description=stage.description,
        kind=stage.kind,
        implemented=stage.implemented,
        planned_phase=stage.planned_phase,
        complete=stage.is_complete(session),
        runnable=(
            stage.implemented
            and stage.kind is StageKind.BATCH
            and all(p.met for p in prerequisites)
        ),
        prerequisites=prerequisites,
        outputs=list(stage.outputs),
        last_run=_LAST_RUNS.get(stage.key),
        open_url=stage.open_url,
    )


def stage_statuses() -> list[StageStatus]:
    """Snapshot every stage (one shared session; degrades when the DB is down)."""
    session = _open_session()
    try:
        return [_status(stage, session) for stage in STAGES]
    finally:
        if session is not None:
            session.close()


def _noop_progress(_message: str) -> None:
    return None


def run_stage(key: str, progress: ProgressCallback = _noop_progress) -> str:
    """Run one stage synchronously; returns its one-line summary.

    Raises ``KeyError`` (unknown), ``StageNotRunnableError`` (unimplemented or
    interactive), ``PrerequisitesUnmetError``, or whatever the stage itself raises.
    """
    from ..db import get_session_factory

    stage = get_stage(key)
    if not stage.implemented or stage.kind is not StageKind.BATCH:
        stage.run(get_session_factory(), progress)  # raises StageNotRunnableError
    session = _open_session()
    try:
        unmet = [p for p in stage.prerequisites(session) if not p.met]
    finally:
        if session is not None:
            session.close()
    if unmet:
        reasons = "; ".join(p.detail or p.label for p in unmet)
        raise PrerequisitesUnmetError(f"'{stage.label}' cannot run yet: {reasons}")

    started = time.monotonic()
    try:
        summary = stage.run(get_session_factory(), progress)
    except Exception as exc:
        _LAST_RUNS[key] = LastRun(
            finished_at=datetime.now(tz=UTC),
            duration_seconds=time.monotonic() - started,
            summary=str(exc),
            ok=False,
        )
        raise
    _LAST_RUNS[key] = LastRun(
        finished_at=datetime.now(tz=UTC),
        duration_seconds=time.monotonic() - started,
        summary=summary,
        ok=True,
    )
    return summary


def run_remaining(progress: ProgressCallback = _noop_progress) -> str:
    """Run every incomplete batch stage in dependency order.

    Completed stages are never rerun; interactive stages are skipped. On
    reaching an incomplete stage that is unimplemented or blocked, stop
    cleanly and report why — that is a successful outcome, not a failure. A
    stage that raises during execution propagates (the run failed).
    """
    ran: list[str] = []
    stopped: str | None = None

    for stage in STAGES:
        if stage.kind is not StageKind.BATCH:
            continue
        session = _open_session()
        try:
            if stage.is_complete(session):
                continue
            unmet = [p for p in stage.prerequisites(session) if not p.met]
        finally:
            if session is not None:
                session.close()
        if not stage.implemented:
            stopped = f"'{stage.label}' is not yet implemented" + (
                f" ({stage.planned_phase})." if stage.planned_phase else "."
            )
            break
        if unmet:
            reasons = "; ".join(p.detail or p.label for p in unmet)
            stopped = f"'{stage.label}' is blocked: {reasons}"
            break
        progress(f"Running {stage.label}…")
        run_stage(stage.key, progress)
        ran.append(stage.label)

    if ran and stopped:
        return f"Ran {len(ran)} stage(s): {', '.join(ran)}. Stopped: {stopped}"
    if ran:
        return f"Ran {len(ran)} stage(s): {', '.join(ran)}. Pipeline is up to date."
    if stopped:
        return f"Nothing ran. Stopped: {stopped}"
    return "All pipeline stages are already complete."
