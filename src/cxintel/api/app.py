"""FastAPI application.

Serves the control-center landing page at ``/`` and a small set of typed JSON
endpoints that back it: ``/api/status`` (service + pipeline status) and
``/api/config`` (non-secret configuration). ``/health`` remains the machine
probe and Swagger stays at ``/docs``. The Resolution Assistant endpoints
(Phase 6) will be added to this module.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse

from .. import __version__
from ..config import get_settings
from ..db import check_health
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
