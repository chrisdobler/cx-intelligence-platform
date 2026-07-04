"""API tests for the pipeline control-center endpoints."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cxintel.api.app import app
from cxintel.pipeline.jobs import JobTracker
from cxintel.repositories import (
    AnomalyRepository,
    ConversationAnalysisRepository,
    ConversationIssueRepository,
    ConversationRepository,
    IssueCatalogRepository,
    MessageRepository,
)

from .test_ingestion import make_record
from .test_pipeline_reset import _seed_derived_artifacts

client = TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_tracker(monkeypatch: pytest.MonkeyPatch) -> JobTracker:
    """A clean, synchronous job tracker for every test."""
    tracker = JobTracker()
    monkeypatch.setattr(tracker, "_spawn", lambda fn: fn())
    monkeypatch.setattr("cxintel.api.app.TRACKER", tracker)
    monkeypatch.setattr("cxintel.api.status.TRACKER", tracker)
    return tracker


@pytest.fixture(autouse=True)
def _unconfigured_ai(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep tests deterministic regardless of any real key in the host .env.

    An empty env var overrides the .env file, and ai_configured treats it as
    unset — so the understand stage is blocked unless a test injects a fake
    provider explicitly.
    """
    from cxintel.config import get_settings

    monkeypatch.setenv("GOOGLE_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
    assert understand["implemented"] is True
    assert understand["planned_phase"] is None
    assert [o["value"] for o in understand["run_options"]] == [
        "sample",
        "full",
        "retry_failures",
    ]
    chat = next(s for s in stages if s["key"] == "resolution_assistant")
    assert chat["action"] == "open"


def test_run_unknown_stage_returns_404() -> None:
    assert client.post("/api/pipeline/nope/run").status_code == 404


def test_run_knowledge_base_blocked_without_ai_key() -> None:
    response = client.post("/api/pipeline/knowledge_base/run")
    assert response.status_code == 422
    assert "GOOGLE_API_KEY" in response.json()["detail"]


def test_run_understand_blocked_without_ai_key() -> None:
    response = client.post("/api/pipeline/understand/run")
    assert response.status_code == 422
    assert "GOOGLE_API_KEY" in response.json()["detail"]


def test_run_with_unknown_option_returns_422() -> None:
    response = client.post("/api/pipeline/understand/run?option=bogus")
    assert response.status_code == 422
    assert "option" in response.json()["detail"].lower()


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


def test_reset_derived_endpoint_returns_fresh_status(
    settings_on_test_db: str, db_session: Any
) -> None:
    _seed_derived_artifacts(db_session)

    before = client.get("/api/status").json()
    assert before["metrics"]["imported_conversations"] == 1
    assert before["metrics"]["processed_conversations"] == 1
    assert before["metrics"]["conversation_issue_count"] == 1
    assert before["metrics"]["issue_catalog_count"] == 1
    assert before["metrics"]["anomaly_count"] == 1
    assert next(s for s in before["pipeline"] if s["key"] == "ingest")["complete"] is True
    assert next(s for s in before["pipeline"] if s["key"] == "understand")["complete"] is True
    assert next(s for s in before["pipeline"] if s["key"] == "anomaly")["complete"] is True

    response = client.post("/api/pipeline/reset-derived")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert ConversationRepository(db_session).count() == 1
    assert MessageRepository(db_session).count() == 1
    assert ConversationAnalysisRepository(db_session).count() == 0
    assert ConversationIssueRepository(db_session).count() == 0
    assert IssueCatalogRepository(db_session).count() == 0
    assert AnomalyRepository(db_session).count() == 0
    assert payload["metrics"]["imported_conversations"] == 1
    assert payload["metrics"]["processed_conversations"] == 0
    assert payload["metrics"]["conversation_issue_count"] == 0
    assert payload["metrics"]["issue_catalog_count"] == 0
    assert payload["metrics"]["anomaly_count"] == 0
    assert next(s for s in payload["pipeline"] if s["key"] == "ingest")["complete"] is True
    assert next(s for s in payload["pipeline"] if s["key"] == "understand")["complete"] is False
    assert next(s for s in payload["pipeline"] if s["key"] == "anomaly")["complete"] is False

    runs = client.get("/api/pipeline/runs").json()
    assert runs[0]["stage_key"] == "reset_derived"
    assert runs[0]["stage_label"] == "Reset Derived Data"
    assert runs[0]["status"] == "succeeded"


def test_reset_derived_endpoint_returns_409_when_busy(
    monkeypatch: pytest.MonkeyPatch, _fresh_tracker: JobTracker
) -> None:
    monkeypatch.setattr(_fresh_tracker, "_spawn", lambda fn: None)
    _fresh_tracker.start("pipeline", lambda progress: "never")
    response = client.post("/api/pipeline/reset-derived")
    assert response.status_code == 409
    assert "still running" in response.json()["detail"]


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
    assert progress["succeeded_work"] == 1
    assert progress["remaining_work"] == 0
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
    # Ran ingest, then stopped cleanly at understand (blocked: AI unconfigured here).
    assert "Data Ingestion" in job["message"]
    assert "blocked" in job["message"]


class _CannedProvider:
    """Returns the frozen V1 example for any prompt."""

    def extract(self, prompt: str, schema: Any, on_retry: Any = None) -> Any:
        from .test_understanding_schema import FROZEN_V1_EXAMPLE

        return schema.model_validate(FROZEN_V1_EXAMPLE)


def test_run_understand_sample_end_to_end(
    tmp_path: Path,
    settings_on_test_db: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Any,
) -> None:
    dataset = tmp_path / "tickets.json"
    dataset.write_text(json.dumps([make_record()]), encoding="utf-8")
    monkeypatch.setenv("RAW_DATA_PATH", str(dataset))
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")  # prerequisites met
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    monkeypatch.setattr("cxintel.llm.get_llm_provider", lambda: _CannedProvider())
    from cxintel.config import get_settings

    get_settings.cache_clear()

    assert client.post("/api/pipeline/ingest/run").status_code == 202
    response = client.post("/api/pipeline/understand/run?option=sample")
    assert response.status_code == 202, response.text

    payload = client.get("/api/status").json()
    assert payload["job"]["state"] == "succeeded"
    assert "Analyzed 1 conversations" in payload["job"]["message"]
    progress = payload["job"]["progress_detail"]
    assert progress["stage_key"] == "understand"
    assert progress["total_work"] == 1
    assert progress["completed_work"] == 1
    assert progress["succeeded_work"] == 1
    assert progress["remaining_work"] == 0
    assert progress["percentage"] == 100
    assert progress["failure_count"] == 0
    understand = next(s for s in payload["pipeline"] if s["key"] == "understand")
    assert understand["complete"] is True  # 1 of 1 analyzed
    assert payload["metrics"]["processed_conversations"] == 1

    observations = client.get("/api/pipeline/llm-observations?sort=llm_seconds").json()
    assert len(observations) == 1
    assert observations[0]["conversation_external_id"] == "conv_0001"
    assert observations[0]["status"] == "succeeded"
    assert observations[0]["pipeline_run_id"] is not None
    assert observations[0]["prompt_characters"] > 0


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
