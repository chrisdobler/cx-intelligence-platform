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
    """One stage of the processing pipeline, rendered as ✔ / ○."""

    key: str
    label: str
    state: StageState


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


def _pipeline_stages() -> list[PipelineStage]:
    """The pipeline stages shown on the control center.

    All stages are ``pending`` in the current phase. As each phase lands, flip
    its ``state`` to ``StageState.DONE`` (later: derive it from a real check).
    """
    return [
        PipelineStage(key="ingest", label="Dataset Imported", state=StageState.PENDING),
        PipelineStage(
            key="understand",
            label="Conversation Understanding",
            state=StageState.PENDING,
        ),
        PipelineStage(key="knowledge_base", label="Knowledge Base", state=StageState.PENDING),
        PipelineStage(key="anomaly", label="Anomaly Detection", state=StageState.PENDING),
        PipelineStage(
            key="resolution_assistant",
            label="Resolution Assistant",
            state=StageState.PENDING,
        ),
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
        metrics=Metrics(),
    )
