"""FastAPI application.

Serves the control-center landing page at ``/`` and a small set of typed JSON
endpoints that back it: ``/api/status`` (service + pipeline status) and
``/api/config`` (non-secret configuration). ``/health`` remains the machine
probe and Swagger stays at ``/docs``. The Resolution Assistant endpoints
(Phase 6) will be added to this module.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import __version__
from ..config import get_settings, set_env_key
from ..db import check_health
from ..pipeline import orchestrator
from ..pipeline.jobs import TRACKER, Job, JobBusyError
from ..pipeline.orchestrator import run_remaining, run_stage
from ..pipeline.stages import StageKind
from .status import PlatformStatus, build_status

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
        "google_api_key_set": s.google_api_key is not None,
        "embedding_provider": s.embedding_provider,
        "embedding_model": s.embedding_model,
        "embedding_dim": s.embedding_dim,
        "slack_webhook_set": s.slack_webhook_url is not None,
        "raw_data_path": s.raw_data_path,
        "batch_size": s.batch_size,
        "log_level": s.log_level,
        "api_host": s.api_host,
        "api_port": s.api_port,
    }


@app.post("/api/pipeline/{key}/run", status_code=202)
def run_pipeline_stage(key: str) -> Job:
    """Run one pipeline stage in the background (202 with the job snapshot)."""
    try:
        stage = orchestrator.get_stage(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown pipeline stage '{key}'.") from None

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
        return TRACKER.start(key, lambda progress: run_stage(key, progress))
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/pipeline/run", status_code=202)
def run_remaining_pipeline() -> Job:
    """Run every incomplete pipeline stage in dependency order, in the background."""
    try:
        return TRACKER.start("pipeline", run_remaining)
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
