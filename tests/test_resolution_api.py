"""API tests for the Phase 6 resolution endpoints and stage status."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cxintel.knowledge_base.service import KnowledgeBaseService

from .test_knowledge_base import FakeEmbedder, seed_knowledge_scenario
from .test_resolution_assistant import FakeProvider, seed_open_leak, ticket_structured


@pytest.fixture
def factory(settings_on_test_db: str, migrated_engine: Any, db_session: Session) -> Any:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False)


@pytest.fixture
def kb(factory: Any, db_session: Session) -> Session:
    seed_knowledge_scenario(db_session)
    KnowledgeBaseService(factory, FakeEmbedder()).run()
    return db_session


@pytest.fixture
def api_env(factory: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    from cxintel.config import get_settings

    provider = FakeProvider(structured=ticket_structured())
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    get_settings.cache_clear()
    monkeypatch.setattr("cxintel.llm.get_llm_provider", lambda **kw: provider)
    monkeypatch.setattr("cxintel.llm.get_embedding_provider", lambda: FakeEmbedder())
    yield provider
    get_settings.cache_clear()


def client() -> Any:
    from fastapi.testclient import TestClient

    from cxintel.api.app import app

    return TestClient(app)


def test_resolution_requires_exactly_one_input(kb: Session, api_env: FakeProvider) -> None:
    both = client().post(
        "/api/resolution", json={"conversation_id": "x", "text": "y"}
    )
    neither = client().post("/api/resolution", json={})
    assert both.status_code == 422
    assert neither.status_code == 422
    assert "exactly one" in both.json()["detail"]


def test_resolution_unconfigured_ai(
    kb: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cxintel.config import get_settings

    monkeypatch.setenv("GOOGLE_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = client().post("/api/resolution", json={"text": "pod leaking"})
    finally:
        get_settings.cache_clear()
    assert response.status_code == 422
    assert "GOOGLE_API_KEY" in response.json()["detail"]


def test_resolution_unknown_conversation(kb: Session, api_env: FakeProvider) -> None:
    response = client().post("/api/resolution", json={"conversation_id": "nope-404"})
    assert response.status_code == 404


def test_resolution_conversation_mode(kb: Session, api_env: FakeProvider) -> None:
    seed_open_leak(kb)
    response = client().post("/api/resolution", json={"conversation_id": "res_open_leak"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "conversation"
    assert body["llm_called"] is True
    assert body["response"]["grounded"] is True
    assert body["response"]["citations"] == ["KB-1"]
    assert body["bundle"]["documents"][0]["doc_id"] == "KB-1"
    assert body["bundle"]["retrieval"]["result_count"] >= 1


def test_resolution_ticket_mode(kb: Session, api_env: FakeProvider) -> None:
    response = client().post(
        "/api/resolution", json={"text": "my pod is leaking water", "product": "Pod 5"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "ticket"
    assert body["conversation_id"] is None
    assert body["response"]["grounded"] is True


def test_resolution_zero_hit_is_success(
    factory: Any, db_session: Session, api_env: FakeProvider
) -> None:
    seed_open_leak(db_session)  # no knowledge base built
    response = client().post("/api/resolution", json={"conversation_id": "res_open_leak"})
    assert response.status_code == 200
    body = response.json()
    assert body["llm_called"] is False
    assert body["response"]["grounded"] is False
    assert body["response"]["evidence_strength"] == "none"


def test_resolution_issues_endpoint(kb: Session, api_env: FakeProvider) -> None:
    from .test_resolution_assistant import seed_two_issue_conversation

    seed_two_issue_conversation(kb)
    response = client().get(
        "/api/resolution/issues", params={"conversation_id": "res_two_issues"}
    )
    assert response.status_code == 200
    names = [o["canonical_name"] for o in response.json()]
    assert names == ["base water leak", "wifi connectivity drop"]

    missing = client().get("/api/resolution/issues", params={"conversation_id": "nope"})
    assert missing.status_code == 404


def test_stage_status_shows_implemented_interactive_stage(
    kb: Session, api_env: FakeProvider
) -> None:
    status = client().get("/api/status").json()
    stage = next(s for s in status["pipeline"] if s["key"] == "resolution_assistant")
    assert stage["implemented"] is True
    assert stage["action"] == "open"
    assert stage["open_url"] == "/#resolution"
    assert stage["planned_phase"] is None
