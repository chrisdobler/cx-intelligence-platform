"""API smoke test — the health endpoint returns 200 even without a database."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cxintel.api.app import app


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


def test_status_endpoint_shape() -> None:
    client = TestClient(app)
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    assert {"services", "ai", "pipeline", "metrics"} <= payload.keys()
    assert len(payload["pipeline"]) == 5
    assert {m.get("name") for m in payload["services"]} == {
        "PostgreSQL",
        "pgvector",
        "FastAPI API",
    }
    # No key configured in the test environment.
    assert payload["ai"]["configured"] is False
    assert payload["ai"]["provider"] == "google"


def test_config_endpoint_hides_secrets() -> None:
    client = TestClient(app)
    response = client.get("/api/config")
    assert response.status_code == 200
    payload = response.json()
    # Secret values must never appear; only booleans reporting whether set.
    assert "google_api_key" not in payload
    assert "slack_webhook_url" not in payload
    assert payload["google_api_key_set"] is False
    # The DB password is masked in the displayed URL.
    assert "***" in payload["database_url"]
    assert ":cx@" not in payload["database_url"]
