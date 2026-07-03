"""API tests for the pipeline control-center endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cxintel.api.app import app
from cxintel.pipeline.jobs import JobTracker

from .test_ingestion import make_record

client = TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_tracker(monkeypatch: pytest.MonkeyPatch) -> JobTracker:
    """A clean, synchronous job tracker for every test."""
    tracker = JobTracker()
    monkeypatch.setattr(tracker, "_spawn", lambda fn: fn())
    monkeypatch.setattr("cxintel.api.app.TRACKER", tracker)
    monkeypatch.setattr("cxintel.api.status.TRACKER", tracker)
    return tracker


def test_status_includes_stage_cards_and_job() -> None:
    payload = client.get("/api/status").json()
    assert "job" in payload
    stages = payload["pipeline"]
    assert len(stages) == 5
    for stage in stages:
        assert {"key", "label", "state", "description", "implemented", "runnable"} <= (stage.keys())
        assert isinstance(stage["prerequisites"], list)
        assert stage["action"] in {"run", "run_again", "open", "none"}
    understand = next(s for s in stages if s["key"] == "understand")
    assert understand["implemented"] is False
    assert understand["planned_phase"] == "Phase 3"
    chat = next(s for s in stages if s["key"] == "resolution_assistant")
    assert chat["action"] == "open"


def test_run_unknown_stage_returns_404() -> None:
    assert client.post("/api/pipeline/nope/run").status_code == 404


def test_run_unimplemented_stage_returns_422() -> None:
    response = client.post("/api/pipeline/understand/run")
    assert response.status_code == 422
    assert "Phase 3" in response.json()["detail"]


def test_run_interactive_stage_returns_422() -> None:
    response = client.post("/api/pipeline/resolution_assistant/run")
    assert response.status_code == 422


def test_run_returns_409_when_busy(
    monkeypatch: pytest.MonkeyPatch, _fresh_tracker: JobTracker
) -> None:
    # A spawn that never executes leaves the job permanently RUNNING.
    monkeypatch.setattr(_fresh_tracker, "_spawn", lambda fn: None)
    _fresh_tracker.start("pipeline", lambda progress: "never")
    assert client.post("/api/pipeline/run").status_code == 409


def test_run_ingest_end_to_end(
    tmp_path: Path,
    settings_on_test_db: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Any,
) -> None:
    dataset = tmp_path / "tickets.json"
    dataset.write_text(json.dumps([make_record()]), encoding="utf-8")
    monkeypatch.setenv("RAW_DATA_PATH", str(dataset))
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)  # alembic.ini
    from cxintel.config import get_settings

    get_settings.cache_clear()

    response = client.post("/api/pipeline/ingest/run")
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["target"] == "ingest"

    payload = client.get("/api/status").json()
    assert payload["job"]["state"] == "succeeded"
    assert "1 conversations" in payload["job"]["message"]
    progress = payload["job"]["progress_detail"]
    assert progress["stage_key"] == "ingest"
    assert progress["total_work"] == 1
    assert progress["completed_work"] == 1
    assert progress["percentage"] == 100
    assert progress["current_item"] == "conv_0001"
    ingest = next(s for s in payload["pipeline"] if s["key"] == "ingest")
    assert ingest["complete"] is True
    assert ingest["state"] == "done"
    assert ingest["action"] == "run_again"
    assert ingest["last_run"]["ok"] is True
    assert payload["metrics"]["imported_conversations"] == 1


def test_run_remaining_end_to_end(
    tmp_path: Path,
    settings_on_test_db: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Any,
) -> None:
    dataset = tmp_path / "tickets.json"
    dataset.write_text(json.dumps([make_record()]), encoding="utf-8")
    monkeypatch.setenv("RAW_DATA_PATH", str(dataset))
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    from cxintel.config import get_settings

    get_settings.cache_clear()

    response = client.post("/api/pipeline/run")
    assert response.status_code == 202, response.text

    job = client.get("/api/status").json()["job"]
    assert job["state"] == "succeeded"
    assert job["target"] == "pipeline"
    # Ran ingest, then stopped cleanly at the unimplemented understand stage.
    assert "Data Ingestion" in job["message"]
    assert "not yet implemented" in job["message"]


def test_runs_endpoint_empty(settings_on_test_db: str, db_session: Any) -> None:
    assert client.get("/api/pipeline/runs").json() == []


def test_runs_endpoint_lists_api_triggered_run(
    tmp_path: Path,
    settings_on_test_db: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Any,
) -> None:
    dataset = tmp_path / "tickets.json"
    dataset.write_text(json.dumps([make_record()]), encoding="utf-8")
    monkeypatch.setenv("RAW_DATA_PATH", str(dataset))
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    from cxintel.config import get_settings

    get_settings.cache_clear()

    assert client.post("/api/pipeline/ingest/run").status_code == 202
    runs = client.get("/api/pipeline/runs").json()
    assert len(runs) == 1
    run = runs[0]
    assert run["stage_key"] == "ingest"
    assert run["stage_label"] == "Data Ingestion"
    assert run["status"] == "succeeded"
    assert run["trigger"] == "api"
    assert run["started_at"] and run["finished_at"]
    assert run["duration_seconds"] >= 0
    assert "1 conversations" in run["summary"]
    assert run["error"] is None

    # limit is honoured
    assert client.post("/api/pipeline/ingest/run").status_code == 202
    assert len(client.get("/api/pipeline/runs?limit=1").json()) == 1


def test_runs_endpoint_degrades_when_db_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from cxintel.config import get_settings
    from cxintel.db import get_engine, get_session_factory

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://cx:cx@localhost:1/cx")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    try:
        response = client.get("/api/pipeline/runs")
        assert response.status_code == 200
        assert response.json() == []
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
