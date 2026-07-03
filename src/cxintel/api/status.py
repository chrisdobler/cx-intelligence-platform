"""Platform status model — the control-center's data source.

The landing page is a thin client over this typed model. ``build_status``
derives service health from the existing :func:`cxintel.db.check_health` probe
and returns a static (for now) description of the pipeline stages plus empty
metrics.

This module is the *single place* later phases touch to make the control center
live: fill in :class:`Metrics` from real queries, and flip a
:class:`PipelineStage` ``state`` to ``done`` once its phase runs. The HTML never
needs to change.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from ..config import get_settings
from ..db import check_health
from ..pipeline.jobs import TRACKER, Job
from ..pipeline.orchestrator import LastRun, StageStatus, stage_statuses
from ..pipeline.stages import Prerequisite, StageKind

AI_SETUP_URL = "https://aistudio.google.com/apikey"


class ServiceState(StrEnum):
    """Traffic-light state for an infrastructure dependency."""

    OK = "ok"  # green
    DEGRADED = "degraded"  # yellow
    DOWN = "down"  # red


class StageState(StrEnum):
    """Completion state for a pipeline stage."""

    DONE = "done"  # ✔
    PENDING = "pending"  # ○


class ServiceStatus(BaseModel):
    """Health of a single service, rendered as a green/yellow/red indicator."""

    name: str
    state: ServiceState
    detail: str | None = None


class PipelineStage(BaseModel):
    """One stage of the processing pipeline — a full control-center card."""

    key: str
    label: str
    state: StageState
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
    action: str  # "run" | "run_again" | "open" | "none"


class Metrics(BaseModel):
    """Headline counts. All ``None`` until the owning phase populates them."""

    imported_conversations: int | None = None  # Phase 2
    processed_conversations: int | None = None  # Phase 3
    embedding_count: int | None = None  # Phase 5
    anomaly_count: int | None = None  # Phase 4


class AIStatus(BaseModel):
    """Whether AI generation is configured, plus how to enable it if not."""

    configured: bool
    provider: str
    model: str
    setup_url: str


class PlatformStatus(BaseModel):
    """Everything the landing page needs in one typed payload."""

    services: list[ServiceStatus] = Field(default_factory=list)
    ai: AIStatus
    pipeline: list[PipelineStage] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)
    job: Job | None = None


def _ingest_metrics() -> Metrics:
    """Headline counts from the database, or empty metrics when unavailable.

    Degrades gracefully (like :func:`cxintel.db.check_health`) so the status
    endpoint keeps working when the database is down or not yet migrated.
    """
    from ..db import get_session_factory
    from ..repositories import ConversationRepository

    try:
        with get_session_factory()() as session:
            return Metrics(imported_conversations=ConversationRepository(session).count())
    except Exception:
        return Metrics()


def _stage_action(status: StageStatus) -> str:
    """The card's primary action, computed server-side so the page stays dumb."""
    if status.kind is StageKind.INTERACTIVE:
        return "open"
    if not status.implemented:
        return "run"  # rendered disabled with the planned-phase reason
    return "run_again" if status.complete else "run"


def _pipeline_stages() -> list[PipelineStage]:
    """The stage cards shown on the control center, from the orchestrator."""
    return [
        PipelineStage(
            key=s.key,
            label=s.label,
            state=StageState.DONE if s.complete else StageState.PENDING,
            description=s.description,
            kind=s.kind,
            implemented=s.implemented,
            planned_phase=s.planned_phase,
            complete=s.complete,
            runnable=s.runnable,
            prerequisites=s.prerequisites,
            outputs=s.outputs,
            last_run=s.last_run,
            open_url=s.open_url,
            action=_stage_action(s),
        )
        for s in stage_statuses()
    ]


def build_status() -> PlatformStatus:
    """Assemble the current platform status from live health probes."""
    health = check_health()

    if health.connected:
        postgres = ServiceStatus(
            name="PostgreSQL",
            state=ServiceState.OK,
            detail=f"server {health.server_version}" if health.server_version else "connected",
        )
        pgvector = ServiceStatus(
            name="pgvector",
            state=ServiceState.OK if health.pgvector_installed else ServiceState.DEGRADED,
            detail="installed" if health.pgvector_installed else "extension not installed",
        )
    else:
        postgres = ServiceStatus(
            name="PostgreSQL",
            state=ServiceState.DOWN,
            detail=health.error or "unreachable",
        )
        pgvector = ServiceStatus(
            name="pgvector",
            state=ServiceState.DOWN,
            detail="database unreachable",
        )

    # The API is, by definition, up if this code is running to answer the
    # request. The client marks it red only when the fetch itself fails.
    api = ServiceStatus(name="FastAPI API", state=ServiceState.OK, detail="serving")

    settings = get_settings()
    ai = AIStatus(
        configured=settings.ai_configured,
        provider=settings.llm_provider,
        model=settings.llm_model,
        setup_url=AI_SETUP_URL,
    )

    return PlatformStatus(
        services=[postgres, pgvector, api],
        ai=ai,
        pipeline=_pipeline_stages(),
        metrics=_ingest_metrics(),
        job=TRACKER.current(),
    )
