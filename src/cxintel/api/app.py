"""FastAPI application.

Phase 1 exposes only ``GET /health``. The Resolution Assistant endpoints
(Phase 6) will be added to this module.
"""

from __future__ import annotations

from fastapi import FastAPI

from .. import __version__
from ..db import check_health

app = FastAPI(title="Conversation Intelligence Platform", version=__version__)


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
