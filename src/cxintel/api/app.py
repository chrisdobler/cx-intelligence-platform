"""FastAPI application.

Serves the control-center landing page at ``/`` and a small set of typed JSON
endpoints that back it: ``/api/status`` (service + pipeline status) and
``/api/config`` (non-secret configuration). ``/health`` remains the machine
probe and Swagger stays at ``/docs``. The Resolution Assistant (Phase 6) is
exposed via ``POST /api/resolution`` and ``GET /api/resolution/issues``.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..config import LLM_MODEL_OPTIONS, SUPPORTED_LLM_MODEL_VALUES, get_settings, set_env_key
from ..db import check_health
from ..knowledge_base.retrieval import RetrievedKnowledge
from ..pipeline import orchestrator
from ..pipeline.jobs import TRACKER, Job, JobBusyError, JobState
from ..pipeline.orchestrator import (
    AnomalyObservationRecord,
    LLMObservationRecord,
    RunRecord,
    anomaly_observations,
    latest_evaluation,
    llm_observations,
    recent_runs,
    run_remaining,
    run_stage,
)
from ..pipeline.reset import reset_derived_data
from ..pipeline.stages import StageKind
from ..resolution_assistant.schema import IssueOption, ResolutionResult
from .status import PlatformStatus, build_status

if TYPE_CHECKING:
    from ..resolution_assistant.service import ResolutionAssistantService

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Conversation Intelligence Platform", version=__version__)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the control-center landing page."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, object]:
    """Liveness/readiness probe: always 200, with best-effort DB status."""
    db = check_health()
    return {
        "status": "ok",
        "version": __version__,
        "database": {
            "connected": db.connected,
            "pgvector": db.pgvector_installed,
            "server_version": db.server_version,
        },
    }


@app.get("/api/status")
def api_status() -> PlatformStatus:
    """Typed status payload consumed by the landing page."""
    return build_status()


def _mask_database_url(url: str) -> str:
    """Redact the password in a SQLAlchemy/DB URL for safe display."""
    parts = urlsplit(url)
    if "@" not in parts.netloc or ":" not in parts.netloc.rsplit("@", 1)[0]:
        return url
    creds, host = parts.netloc.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return urlunsplit(parts._replace(netloc=f"{user}:***@{host}"))


@app.get("/api/config")
def api_config() -> dict[str, object]:
    """Non-secret configuration, for reviewers inspecting the environment.

    Secret values are never emitted — only booleans indicating whether they
    are set.
    """
    s = get_settings()
    return {
        "version": __version__,
        "database_url": _mask_database_url(s.database_url),
        "llm_provider": s.llm_provider,
        "llm_model": s.llm_model,
        "llm_model_options": [
            {"label": option.label, "value": option.value} for option in LLM_MODEL_OPTIONS
        ],
        "google_api_key_set": s.google_api_key is not None,
        "embedding_provider": s.embedding_provider,
        "embedding_model": s.embedding_model,
        "embedding_dim": s.embedding_dim,
        "slack_webhook_set": s.slack_webhook_url is not None,
        "raw_data_path": s.raw_data_path,
        "derived_data_path": s.derived_data_path,
        "batch_size": s.batch_size,
        "log_level": s.log_level,
        "api_host": s.api_host,
        "api_port": s.api_port,
    }


@app.get("/api/pipeline/runs")
def pipeline_runs(limit: int = Query(default=20, ge=1, le=200)) -> list[RunRecord]:
    """The pipeline audit trail — recent runs, newest first ([] when the DB is down)."""
    return recent_runs(limit=limit)


class AnomalyRecord(BaseModel):
    """One canonical anomaly, as exposed by the API."""

    issue: str
    day: int
    observation_date: datetime | None
    baseline_date: datetime | None
    severity: str
    signals: list[str]
    metrics: dict[str, float | int | None]
    summary: str
    recommended_action: str
    slack_message: str


def _anomaly_rows() -> list[AnomalyRecord]:
    from ..db import get_session_factory
    from ..repositories import AnomalyRepository

    try:
        with get_session_factory()() as session:
            rows = AnomalyRepository(session).all()
    except Exception:
        return []
    return [
        AnomalyRecord(
            issue=a.issue,
            day=a.day,
            observation_date=a.observation_date,
            baseline_date=a.baseline_date,
            severity=a.severity,
            signals=a.signals,
            metrics=a.metrics,
            summary=a.description,
            recommended_action=a.recommended_action,
            slack_message=a.slack_message,
        )
        for a in rows
    ]


@app.get("/api/anomalies")
def api_anomalies() -> list[AnomalyRecord]:
    """Detected anomalies (the canonical Phase 4 artifact); [] when the DB is down."""
    return _anomaly_rows()


class TimelinePoint(BaseModel):
    """One time bucket on the anomaly timeline."""

    t: datetime
    count: int


class AnomalyTimeline(BaseModel):
    """Hourly issue-frequency timeline behind the control-center chart."""

    issue: str
    bucket_seconds: int
    points: list[TimelinePoint]  # zero-filled between first and last bucket
    day_starts: dict[str, datetime]  # reporting day → first conversation start


# V1 renders hourly buckets; the size is a code-level knob, not a UI control.
_TIMELINE_BUCKET_SECONDS = 3600


@app.get("/api/anomalies/timeline")
def api_anomaly_timeline(issue: str) -> AnomalyTimeline:
    """Occurrences of one anomaly issue over time, in hourly buckets.

    Presentation data only — anomaly detection still operates on its existing
    aggregation periods; this exposes the real ``Conversation.started_at``
    timestamps behind an anomaly. Buckets between the first and last
    occurrence are zero-filled so quiet hours render honestly. Empty when the
    issue is unknown or the database is unavailable.
    """
    from datetime import timedelta

    from ..db import get_session_factory
    from ..repositories import ConversationIssueRepository, ConversationRepository

    empty = AnomalyTimeline(
        issue=issue, bucket_seconds=_TIMELINE_BUCKET_SECONDS, points=[], day_starts={}
    )
    try:
        with get_session_factory()() as session:
            buckets = ConversationIssueRepository(session).issue_timeline(
                issue, bucket_seconds=_TIMELINE_BUCKET_SECONDS
            )
            if not buckets:
                return empty
            day_starts = ConversationRepository(session).day_starts()
    except Exception:
        return empty
    counts = dict(buckets)
    step = timedelta(seconds=_TIMELINE_BUCKET_SECONDS)
    points, cursor = [], buckets[0][0]
    while cursor <= buckets[-1][0]:
        points.append(TimelinePoint(t=cursor, count=counts.get(cursor, 0)))
        cursor += step
    return AnomalyTimeline(
        issue=issue,
        bucket_seconds=_TIMELINE_BUCKET_SECONDS,
        points=points,
        day_starts={str(day): start for day, start in day_starts.items()},
    )


@app.get("/api/anomalies/report", response_class=PlainTextResponse)
def api_anomaly_report() -> str:
    """The anomaly report, rendered from persisted anomalies (markdown)."""
    from ..anomaly.reporting import render_report
    from ..db import get_session_factory
    from ..repositories import AnomalyRepository

    try:
        with get_session_factory()() as session:
            return render_report(AnomalyRepository(session).all())
    except Exception:
        return "# Anomaly Report\n\nDatabase unavailable.\n"


@app.get("/api/knowledge/search")
def api_knowledge_search(
    q: str = Query(min_length=1, description="Natural-language problem description."),
    product: str | None = Query(default=None),
    limit: int = Query(default=5, ge=1, le=20),
) -> list[RetrievedKnowledge]:
    """Metadata-first semantic search over the knowledge base.

    Returns [] when the knowledge base is empty; 422 when AI is unconfigured
    (the query itself must be embedded); 503 when the database is unreachable.
    """
    from ..db import get_session_factory
    from ..knowledge_base.retrieval import retrieve
    from ..llm import LLMExtractionError, get_embedding_provider

    try:
        embedder = get_embedding_provider()
    except LLMExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        with get_session_factory()() as session:
            return retrieve(session, embedder, q, product=product, limit=limit)
    except LLMExtractionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc


class ResolutionRequest(BaseModel):
    """Body for POST /api/resolution — exactly one of conversation_id or text."""

    conversation_id: str | None = None  # UUID or external id (e.g. TICKET-0042)
    text: str | None = None  # free-text new ticket
    product: str | None = None  # ticket mode only: product hint
    issue_index: int | None = None
    limit: int = Field(default=5, ge=1, le=20)


def _resolution_service() -> ResolutionAssistantService:
    from ..db import get_session_factory
    from ..llm import LLMExtractionError, get_embedding_provider, get_llm_provider
    from ..resolution_assistant.service import ResolutionAssistantService

    try:
        return ResolutionAssistantService(
            get_session_factory(), get_llm_provider(), get_embedding_provider()
        )
    except LLMExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/resolution")
def api_resolution(body: ResolutionRequest) -> ResolutionResult:
    """Grounded resolution recommendation for one issue (Phase 6).

    Accepts either an already-analyzed conversation (``conversation_id``) or a
    free-text new ticket (``text``, structured via Prompt #1 and never
    persisted). A zero-hit ungrounded response is a successful outcome (200).
    """
    from ..llm import LLMExtractionError
    from ..resolution_assistant.service import (
        ConversationNotFoundError,
        NoIssuesFoundError,
        UnknownIssueIndexError,
    )

    if (body.conversation_id is None) == (body.text is None):
        raise HTTPException(
            status_code=422, detail="Provide exactly one of 'conversation_id' or 'text'."
        )
    service = _resolution_service()
    try:
        if body.conversation_id is not None:
            return service.resolve_conversation(
                body.conversation_id, issue_index=body.issue_index, limit=body.limit
            )
        assert body.text is not None
        return service.resolve_ticket(
            body.text, product=body.product, issue_index=body.issue_index, limit=body.limit
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (NoIssuesFoundError, UnknownIssueIndexError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LLMExtractionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc


@app.get("/api/resolution/issues")
def api_resolution_issues(
    conversation_id: str = Query(min_length=1, description="UUID or external id."),
) -> list[IssueOption]:
    """The selectable issues of one analyzed conversation (no retrieval, no LLM)."""
    from ..resolution_assistant.service import ConversationNotFoundError

    service = _resolution_service()
    try:
        return service.conversation_issues(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc


@app.get("/api/pipeline/llm-observations")
def pipeline_llm_observations(
    limit: int = Query(default=20, ge=1, le=200),
    sort: str = Query(default="total_seconds"),
    pipeline_run_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[LLMObservationRecord]:
    """Slowest per-conversation LLM timing observations."""
    try:
        return llm_observations(limit=limit, sort=sort, pipeline_run_id=pipeline_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/pipeline/anomaly-observations")
def pipeline_anomaly_observations(
    limit: int = Query(default=20, ge=1, le=200),
    sort: str = Query(default="total_seconds"),
    pipeline_run_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[AnomalyObservationRecord]:
    """Slowest anomaly-detection stage timing observations."""
    try:
        return anomaly_observations(limit=limit, sort=sort, pipeline_run_id=pipeline_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class EvaluationSuiteSummary(BaseModel):
    """Pass/fail totals of one evaluation suite."""

    suite: str
    total: int
    passed: int
    pass_rate: float


class EvaluationStatus(BaseModel):
    """Headline view of the most recent evaluation run (Control Center panel)."""

    available: bool = Field(description="False when no evaluation has run yet.")
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    dataset_version: str | None = None
    model: str | None = None
    embedding_model: str | None = None
    understanding_prompt_version: str | None = None
    resolution_prompt_version: str | None = None
    total_cases: int | None = None
    passed_cases: int | None = None
    pass_rate: float | None = None
    suites: list[EvaluationSuiteSummary] = Field(default_factory=list)
    baseline_available: bool = False
    regression_count: int | None = None
    regressions: list[str] = Field(default_factory=list)
    retrieval_metrics: dict[str, float] | None = None
    grounding_metrics: dict[str, float] | None = None
    total_tokens: int | None = None
    failed_case_ids: list[str] = Field(default_factory=list)


@app.get("/api/evaluation/latest")
def api_evaluation_latest() -> EvaluationStatus:
    """The most recent evaluation run's headline numbers (Phase 7)."""
    record = latest_evaluation()
    if record is None:
        return EvaluationStatus(available=False)
    report = record.report
    suites = [
        EvaluationSuiteSummary(
            suite=suite,
            total=summary.get("total", 0),
            passed=summary.get("passed", 0),
            pass_rate=summary.get("pass_rate", 0.0),
        )
        for suite, summary in (report.get("summary") or {}).items()
    ]
    return EvaluationStatus(
        available=True,
        finished_at=record.finished_at,
        duration_seconds=record.duration_seconds,
        dataset_version=record.dataset_version,
        model=record.model,
        embedding_model=record.embedding_model,
        understanding_prompt_version=record.understanding_prompt_version,
        resolution_prompt_version=record.resolution_prompt_version,
        total_cases=record.total_cases,
        passed_cases=record.passed_cases,
        pass_rate=record.pass_rate,
        suites=suites,
        baseline_available=report.get("baseline") is not None,
        regression_count=record.regression_count,
        regressions=[r.get("detail", "") for r in (report.get("regressions") or [])],
        retrieval_metrics=record.retrieval_metrics,
        grounding_metrics=record.grounding_metrics,
        total_tokens=record.total_tokens,
        failed_case_ids=[
            case.get("case_id", "")
            for case in (report.get("cases") or [])
            if not case.get("passed", False)
        ],
    )


@app.get("/api/evaluation/report", response_class=PlainTextResponse)
def api_evaluation_report() -> str:
    """The latest evaluation report (markdown), rendered from the stored run."""
    from ..evaluation.report import EvaluationReport, render_markdown

    record = latest_evaluation()
    if record is None:
        return "# Evaluation Report\n\nNo evaluation has run yet — run 'app evaluate'.\n"
    try:
        return render_markdown(EvaluationReport.model_validate(record.report))
    except Exception:
        return "# Evaluation Report\n\nStored report could not be rendered.\n"


@app.post("/api/pipeline/{key}/run", status_code=202)
def run_pipeline_stage(key: str, option: str | None = Query(default=None)) -> Job:
    """Run one pipeline stage in the background (202 with the job snapshot).

    ``option`` selects one of the stage's declared run options (e.g. the
    Understanding stage's ``sample`` vs ``full``); omitted = stage default.
    """
    from ..pipeline.stages import StageNotRunnableError

    try:
        stage = orchestrator.get_stage(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown pipeline stage '{key}'.") from None

    try:
        orchestrator.validate_option(stage, option)
    except StageNotRunnableError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if stage.kind is StageKind.INTERACTIVE:
        raise HTTPException(
            status_code=422, detail=f"'{stage.label}' is interactive — open it instead."
        )
    if not stage.implemented:
        raise HTTPException(
            status_code=422,
            detail=f"'{stage.label}' is not yet implemented"
            + (f" (planned for {stage.planned_phase})." if stage.planned_phase else "."),
        )
    unmet = [s for s in orchestrator.stage_statuses() if s.key == key and not s.runnable]
    if unmet:
        reasons = "; ".join(p.detail or p.label for p in unmet[0].prerequisites if not p.met)
        raise HTTPException(status_code=422, detail=f"'{stage.label}' cannot run yet: {reasons}")

    try:
        return TRACKER.start(key, lambda progress: run_stage(key, progress, "api", option))
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/pipeline/run", status_code=202)
def run_remaining_pipeline() -> Job:
    """Run every incomplete pipeline stage in dependency order, in the background."""
    try:
        return TRACKER.start("pipeline", run_remaining)
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/pipeline/import-derived", status_code=202)
def import_derived_pipeline_data() -> Job:
    """Restore the configured pre-generated AI dataset in the background."""
    from ..pipeline.import_derived import import_derived_data

    path = Path(get_settings().derived_data_path)
    try:
        return TRACKER.start(
            "import_derived",
            lambda progress: import_derived_data(path, progress=progress, trigger="api"),
        )
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/pipeline/reset-derived")
def reset_derived_pipeline_data() -> PlatformStatus:
    """Clear regenerable AI artifacts while preserving imported source data."""
    current = TRACKER.current()
    if current is not None and current.state is JobState.RUNNING:
        raise HTTPException(status_code=409, detail=f"'{current.target}' is still running.")
    reset_derived_data(trigger="api")
    return build_status()


class GoogleKeyRequest(BaseModel):
    """Body for the onboarding save-key endpoint. The key is never echoed back."""

    api_key: str


@app.post("/api/config/google-key")
def set_google_key(body: GoogleKeyRequest) -> dict[str, object]:
    """Save the Google AI Studio key from the landing-page onboarding card.

    Writes the key to the local ``.env`` only and makes it live in-process
    (env var + settings-cache clear), so AI capabilities enable without a
    restart. The response reports status booleans only — never the key.
    """
    key = body.api_key.strip()
    if not key or not key.isprintable() or " " in key:
        raise HTTPException(status_code=422, detail="API key must be a single non-empty token.")
    set_env_key("GOOGLE_API_KEY", key)
    os.environ["GOOGLE_API_KEY"] = key
    get_settings.cache_clear()
    return {"saved": True, "ai_configured": get_settings().ai_configured}


class LLMModelRequest(BaseModel):
    """Body for the local model selector endpoint."""

    model: str


@app.post("/api/config/llm-model")
def set_llm_model(body: LLMModelRequest) -> dict[str, object]:
    """Save the reviewer-selected Conversation Understanding model locally."""
    model = body.model.strip()
    if model not in SUPPORTED_LLM_MODEL_VALUES:
        allowed = ", ".join(sorted(SUPPORTED_LLM_MODEL_VALUES))
        raise HTTPException(status_code=422, detail=f"Unsupported LLM model. Choose: {allowed}.")
    set_env_key("LLM_MODEL", model)
    os.environ["LLM_MODEL"] = model
    get_settings.cache_clear()
    return {
        "saved": True,
        "llm_model": get_settings().llm_model,
        "llm_model_options": [
            {"label": option.label, "value": option.value} for option in LLM_MODEL_OPTIONS
        ],
    }
