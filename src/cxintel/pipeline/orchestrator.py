"""Pipeline orchestrator — the single entry point for running stages.

The CLI (``app ingest`` / ``app pipeline``), the REST API
(``POST /api/pipeline/…``), and the landing-page control center all call this
module, so stage behaviour is defined exactly once. ``STAGES`` is ordered by
dependency (the pipeline is linear, so list order is a valid topological
order); each stage still declares its own explicit prerequisites.

Every stage execution is recorded in the ``pipeline_runs`` table — the
run-level audit trail. A ``running`` row is written before the stage executes
(so a crashed process leaves evidence) and finalized when it finishes; both
writes are best-effort so an audit-write failure never breaks a run (on a
fresh database the ingest run itself creates the table via migrations).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from .progress import ProgressCallback
from .stages import (
    AnomalyStage,
    EvaluateStage,
    IngestStage,
    KnowledgeBaseStage,
    PipelineStage,
    Prerequisite,
    ResolutionAssistantStage,
    RunOption,
    StageKind,
    StageNotRunnableError,
    UnderstandStage,
)

STAGES: tuple[PipelineStage, ...] = (
    IngestStage(),
    UnderstandStage(),
    AnomalyStage(),
    KnowledgeBaseStage(),
    ResolutionAssistantStage(),
    EvaluateStage(),
)


class PrerequisitesUnmetError(Exception):
    """Raised when a stage is asked to run before its prerequisites are met."""


class LastRun(BaseModel):
    """A stage's most recent finished execution, read from ``pipeline_runs``."""

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
    run_options: list[RunOption]


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


def _record_start(key: str, trigger: str, started_at: datetime) -> uuid.UUID | None:
    """Best-effort insert of a ``running`` audit row; None when it can't be written."""
    from ..db import get_session_factory
    from ..models import PipelineRun
    from ..repositories import PipelineRunRepository

    try:
        with get_session_factory()() as session:
            run = PipelineRun(
                id=uuid.uuid4(),
                stage_key=key,
                status="running",
                trigger=trigger,
                started_at=started_at,
            )
            PipelineRunRepository(session).add(run)
            session.commit()
            return run.id
    except Exception:
        return None


def _record_finish(
    run_id: uuid.UUID | None,
    key: str,
    trigger: str,
    started_at: datetime,
    duration_seconds: float,
    *,
    summary: str | None = None,
    error: str | None = None,
) -> None:
    """Best-effort finalization of the audit row (inserted whole if start failed)."""
    from ..db import get_session_factory
    from ..models import PipelineRun
    from ..repositories import PipelineRunRepository

    try:
        with get_session_factory()() as session:
            repo = PipelineRunRepository(session)
            run = repo.get(run_id) if run_id is not None else None
            if run is None:
                run = PipelineRun(
                    id=uuid.uuid4(), stage_key=key, trigger=trigger, started_at=started_at
                )
                repo.add(run)
            run.status = "succeeded" if error is None else "failed"
            run.finished_at = datetime.now(tz=UTC)
            run.duration_seconds = duration_seconds
            run.summary = summary
            run.error = error
            session.commit()
    except Exception:
        return None


def _last_runs(session: Session | None) -> dict[str, LastRun]:
    """The most recent finished run per stage, or {} when the DB is unavailable."""
    from ..repositories import PipelineRunRepository

    if session is None:
        return {}
    try:
        rows = PipelineRunRepository(session).latest_finished_per_stage()
    except Exception:
        return {}
    return {
        key: LastRun(
            finished_at=run.finished_at,  # never None: finished rows only
            duration_seconds=run.duration_seconds or 0.0,
            summary=(run.summary if run.status == "succeeded" else run.error) or "",
            ok=run.status == "succeeded",
        )
        for key, run in rows.items()
    }


def _status(stage: PipelineStage, session: Session | None, last_run: LastRun | None) -> StageStatus:
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
        last_run=last_run,
        open_url=stage.open_url,
        run_options=list(stage.run_options),
    )


def stage_statuses() -> list[StageStatus]:
    """Snapshot every stage (one shared session; degrades when the DB is down)."""
    session = _open_session()
    try:
        last_runs = _last_runs(session)
        return [_status(stage, session, last_runs.get(stage.key)) for stage in STAGES]
    finally:
        if session is not None:
            session.close()


class RunRecord(BaseModel):
    """One audit-trail entry, as exposed by the API and the ``app runs`` CLI."""

    id: uuid.UUID
    stage_key: str
    stage_label: str
    status: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    summary: str | None
    error: str | None


class LLMObservationRecord(BaseModel):
    """One per-conversation LLM timing observation."""

    id: uuid.UUID
    pipeline_run_id: uuid.UUID | None
    conversation_id: uuid.UUID
    conversation_external_id: str | None
    day: int
    model: str
    prompt_version: str
    status: str
    total_seconds: float
    load_seconds: float
    prompt_seconds: float
    llm_seconds: float
    persist_seconds: float
    message_count: int
    prompt_characters: int
    issue_count: int
    retry_count: int
    started_at: datetime
    finished_at: datetime
    error: str | None


class AnomalyObservationRecord(BaseModel):
    """One anomaly-detection stage timing observation."""

    id: uuid.UUID
    pipeline_run_id: uuid.UUID | None
    step: str
    day: int | None
    issue: str | None
    status: str
    total_seconds: float
    baseline_issue_count: int
    current_issue_count: int
    anomalies_detected: int
    alert_count: int
    fallback_count: int
    delivered_count: int
    details: dict[str, object]
    started_at: datetime
    finished_at: datetime
    error: str | None


def recent_runs(limit: int = 20) -> list[RunRecord]:
    """The most recent pipeline runs, newest first ([] when the DB is down).

    Labels are resolved from the current registry; audit rows for stages that
    no longer exist keep their key as the label.
    """
    from ..repositories import PipelineRunRepository

    session = _open_session()
    if session is None:
        return []
    try:
        rows = PipelineRunRepository(session).recent(limit=limit)
    except Exception:
        return []
    finally:
        session.close()

    def label(key: str) -> str:
        from .import_derived import IMPORT_DERIVED_STAGE_KEY, IMPORT_DERIVED_STAGE_LABEL
        from .reset import RESET_DERIVED_STAGE_KEY

        if key == IMPORT_DERIVED_STAGE_KEY:
            return IMPORT_DERIVED_STAGE_LABEL
        if key == RESET_DERIVED_STAGE_KEY:
            return "Reset Derived Data"
        try:
            return get_stage(key).label
        except KeyError:
            return key

    return [
        RunRecord(
            id=run.id,
            stage_key=run.stage_key,
            stage_label=label(run.stage_key),
            status=run.status,
            trigger=run.trigger,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=run.duration_seconds,
            summary=run.summary,
            error=run.error,
        )
        for run in rows
    ]


def llm_observations(
    *,
    limit: int = 20,
    sort: str = "total_seconds",
    pipeline_run_id: uuid.UUID | None = None,
) -> list[LLMObservationRecord]:
    """Slowest per-conversation LLM observations ([] when the DB is unavailable)."""
    from ..models import Conversation
    from ..repositories import LLM_OBSERVATION_SORT_FIELDS, LLMCallObservationRepository

    if sort not in LLM_OBSERVATION_SORT_FIELDS:
        raise ValueError(f"Unsupported LLM observation sort '{sort}'.")

    session = _open_session()
    if session is None:
        return []
    try:
        observations = LLMCallObservationRepository(session).slowest(
            limit=limit, sort=sort, pipeline_run_id=pipeline_run_id
        )
        labels = {
            row.id: row.external_id
            for row in session.query(Conversation)
            .filter(Conversation.id.in_([o.conversation_id for o in observations]))
            .all()
        }
    except Exception:
        return []
    finally:
        session.close()

    return [
        LLMObservationRecord(
            id=obs.id,
            pipeline_run_id=obs.pipeline_run_id,
            conversation_id=obs.conversation_id,
            conversation_external_id=labels.get(obs.conversation_id),
            day=obs.day,
            model=obs.model,
            prompt_version=obs.prompt_version,
            status=obs.status,
            total_seconds=obs.total_seconds,
            load_seconds=obs.load_seconds,
            prompt_seconds=obs.prompt_seconds,
            llm_seconds=obs.llm_seconds,
            persist_seconds=obs.persist_seconds,
            message_count=obs.message_count,
            prompt_characters=obs.prompt_characters,
            issue_count=obs.issue_count,
            retry_count=obs.retry_count,
            started_at=obs.started_at,
            finished_at=obs.finished_at,
            error=obs.error,
        )
        for obs in observations
    ]


def anomaly_observations(
    *,
    limit: int = 20,
    sort: str = "total_seconds",
    pipeline_run_id: uuid.UUID | None = None,
) -> list[AnomalyObservationRecord]:
    """Slowest anomaly-detection observations ([] when the DB is unavailable)."""
    from ..repositories import (
        ANOMALY_OBSERVATION_SORT_FIELDS,
        AnomalyStageObservationRepository,
    )

    if sort not in ANOMALY_OBSERVATION_SORT_FIELDS:
        raise ValueError(f"Unsupported anomaly observation sort '{sort}'.")

    session = _open_session()
    if session is None:
        return []
    try:
        observations = AnomalyStageObservationRepository(session).slowest(
            limit=limit, sort=sort, pipeline_run_id=pipeline_run_id
        )
    except Exception:
        return []
    finally:
        session.close()

    return [
        AnomalyObservationRecord(
            id=obs.id,
            pipeline_run_id=obs.pipeline_run_id,
            step=obs.step,
            day=obs.day,
            issue=obs.issue,
            status=obs.status,
            total_seconds=obs.total_seconds,
            baseline_issue_count=obs.baseline_issue_count,
            current_issue_count=obs.current_issue_count,
            anomalies_detected=obs.anomalies_detected,
            alert_count=obs.alert_count,
            fallback_count=obs.fallback_count,
            delivered_count=obs.delivered_count,
            details=obs.details,
            started_at=obs.started_at,
            finished_at=obs.finished_at,
            error=obs.error,
        )
        for obs in observations
    ]


class EvaluationRunRecord(BaseModel):
    """One evaluation run's headline numbers (the full report stays in JSONB)."""

    id: uuid.UUID
    pipeline_run_id: uuid.UUID | None
    dataset_version: str
    model: str
    embedding_model: str
    understanding_prompt_version: str
    resolution_prompt_version: str
    suites: list[str]
    status: str
    total_cases: int
    passed_cases: int
    pass_rate: float
    regression_count: int
    retrieval_metrics: dict[str, Any] | None
    grounding_metrics: dict[str, Any] | None
    total_tokens: int | None
    report: dict[str, Any]
    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    error: str | None


def _evaluation_run_record(run: Any) -> EvaluationRunRecord:
    """Serialize an evaluation_runs ORM row for API/control-center use."""
    return EvaluationRunRecord(
        id=run.id,
        pipeline_run_id=run.pipeline_run_id,
        dataset_version=run.dataset_version,
        model=run.model,
        embedding_model=run.embedding_model,
        understanding_prompt_version=run.understanding_prompt_version,
        resolution_prompt_version=run.resolution_prompt_version,
        suites=run.suites,
        status=run.status,
        total_cases=run.total_cases,
        passed_cases=run.passed_cases,
        pass_rate=run.pass_rate,
        regression_count=run.regression_count,
        retrieval_metrics=run.retrieval_metrics,
        grounding_metrics=run.grounding_metrics,
        total_tokens=run.total_tokens,
        report=run.report,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=run.duration_seconds,
        error=run.error,
    )


def latest_evaluation() -> EvaluationRunRecord | None:
    """The most recent evaluation run (None when none exist or the DB is down)."""
    from ..repositories import EvaluationRunRepository

    session = _open_session()
    if session is None:
        return None
    try:
        run = EvaluationRunRepository(session).latest()
    except Exception:
        return None
    finally:
        session.close()
    if run is None:
        return None
    return _evaluation_run_record(run)


def recent_evaluations(limit: int = 20) -> list[EvaluationRunRecord]:
    """Recent evaluation runs, chronological for trend rendering."""
    from ..repositories import EvaluationRunRepository

    session = _open_session()
    if session is None:
        return []
    try:
        runs = EvaluationRunRepository(session).recent(limit=limit)
    except Exception:
        return []
    finally:
        session.close()
    return [_evaluation_run_record(run) for run in reversed(runs)]


def _noop_progress(_message: object) -> None:
    return None


def validate_option(stage: PipelineStage, option: str | None) -> None:
    """Reject a run option the stage does not declare."""
    if option is None:
        return
    if option not in {o.value for o in stage.run_options}:
        raise StageNotRunnableError(f"'{stage.label}' has no run option '{option}'.")


def run_stage(
    key: str,
    progress: ProgressCallback = _noop_progress,
    trigger: str = "api",
    option: str | None = None,
) -> str:
    """Run one stage synchronously; returns its one-line summary.

    ``option`` selects one of the stage's declared run options (None = the
    stage default). The execution is recorded in the ``pipeline_runs`` audit
    trail with its ``trigger`` source. Raises ``KeyError`` (unknown),
    ``StageNotRunnableError`` (unimplemented, interactive, or unknown option),
    ``PrerequisitesUnmetError``, or whatever the stage itself raises.
    """
    from ..db import get_session_factory

    stage = get_stage(key)
    validate_option(stage, option)
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

    started_at = datetime.now(tz=UTC)
    started = time.monotonic()
    run_id = _record_start(key, trigger, started_at)
    try:
        summary = stage.run(get_session_factory(), progress, option, run_id)
    except Exception as exc:
        _record_finish(run_id, key, trigger, started_at, time.monotonic() - started, error=str(exc))
        raise
    _record_finish(run_id, key, trigger, started_at, time.monotonic() - started, summary=summary)
    return summary


def run_remaining(progress: ProgressCallback = _noop_progress, trigger: str = "api") -> str:
    """Run every incomplete batch stage in dependency order.

    Completed stages are never rerun; interactive stages are skipped. On
    reaching an incomplete stage that is unimplemented or blocked, stop
    cleanly and report why — that is a successful outcome, not a failure. A
    stage that raises during execution propagates (the run failed).
    """
    ran: list[str] = []
    stopped: str | None = None

    for stage in STAGES:
        if stage.kind is not StageKind.BATCH or not stage.include_in_run_remaining:
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
        run_stage(stage.key, progress, trigger)
        ran.append(stage.label)

    if ran and stopped:
        return f"Ran {len(ran)} stage(s): {', '.join(ran)}. Stopped: {stopped}"
    if ran:
        return f"Ran {len(ran)} stage(s): {', '.join(ran)}. Pipeline is up to date."
    if stopped:
        return f"Nothing ran. Stopped: {stopped}"
    return "All pipeline stages are already complete."
