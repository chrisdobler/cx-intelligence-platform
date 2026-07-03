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
