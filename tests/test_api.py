"""API smoke test — the health endpoint returns 200 even without a database."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cxintel.api.app import app
from cxintel.config import get_settings


@pytest.fixture(autouse=True)
def _unconfigured_ai(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate from the developer's real .env — these tests assume no API key."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_health_endpoint_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "database" in payload


def test_landing_page_served_at_root() -> None:
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Conversation Intelligence Platform" in response.text
    assert "Danger Zone" in response.text
    assert "Reset Derived Data" in response.text
    assert "Anomaly Analysis" in response.text
    assert 'id="model-select"' in response.text
    assert "/api/config/llm-model" in response.text
    assert 'opt.value !== "retry_failures"' in response.text


def test_status_endpoint_shape() -> None:
    client = TestClient(app)
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    assert {"services", "ai", "pipeline", "metrics"} <= payload.keys()
    assert len(payload["pipeline"]) == 6
    assert {m.get("name") for m in payload["services"]} == {
        "PostgreSQL",
        "pgvector",
        "FastAPI API",
    }
    # No key configured in the test environment.
    assert payload["ai"]["configured"] is False
    assert payload["ai"]["provider"] == "google"
    assert payload["ai"]["model"] == "gemini-2.5-flash"
    assert payload["ai"]["model_options"] == [
        {"label": "Gemini Flash", "value": "gemini-2.5-flash"},
        {"label": "Gemini Flash-Lite", "value": "gemini-2.5-flash-lite"},
    ]


def test_config_endpoint_hides_secrets() -> None:
    client = TestClient(app)
    response = client.get("/api/config")
    assert response.status_code == 200
    payload = response.json()
    # Secret values must never appear; only booleans reporting whether set.
    assert "google_api_key" not in payload
    assert "slack_webhook_url" not in payload
    assert payload["google_api_key_set"] is False
    assert payload["llm_model"] == "gemini-2.5-flash"
    assert payload["llm_model_options"] == [
        {"label": "Gemini Flash", "value": "gemini-2.5-flash"},
        {"label": "Gemini Flash-Lite", "value": "gemini-2.5-flash-lite"},
    ]
    # The DB password is masked in the displayed URL.
    assert "***" in payload["database_url"]
    assert ":cx@" not in payload["database_url"]
