"""DB-backed tests for knowledge base generation and retrieval (fake embedder)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cxintel.knowledge_base.retrieval import retrieve
from cxintel.knowledge_base.service import KnowledgeBaseService
from cxintel.models import ConversationAnalysis
from cxintel.repositories import KnowledgeDocumentRepository

from .test_knowledge_generation import make_issue, make_structured
from .test_understanding import seed_conversation

DIM = 3072
_AXES = {"leak": 0, "wifi": 1, "heating": 2}


def fake_vector(text: str) -> list[float]:
    """Deterministic embedding: a unit axis per known topic keyword."""
    vector = [0.0] * DIM
    for keyword, axis in _AXES.items():
        if keyword in text.lower():
            vector[axis] = 1.0
    if not any(vector):
        vector[-1] = 1.0
    return vector


class FakeEmbedder:
    """Deterministic embedding provider that records every call."""

    def __init__(self) -> None:
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return [fake_vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return fake_vector(text)


def seed_analysis(
    session: Session,
    external_id: str,
    structured: Any,
    *,
    day: int = 1,
) -> None:
    conv_id = seed_conversation(session, external_id, day, f"text {external_id}")
    upsert_analysis(session, conv_id, structured)


def upsert_analysis(session: Session, conv_id: Any, structured: Any) -> None:
    session.merge(
        ConversationAnalysis(
            conversation_id=conv_id,
            model="fake",
            model_version="fake",
            prompt_version="1.1",
            processed_at=datetime(2026, 7, 3, tzinfo=UTC),
            analysis_json=structured.model_dump(mode="json"),
        )
    )
    session.commit()


def seed_knowledge_scenario(session: Session) -> None:
    """Three conversations: leak (resolved), wifi (resolved), heating (unresolved)."""
    seed_analysis(
        session,
        "kb_leak",
        make_structured(
            [make_issue("base water leak", resolution_summary="replaced the base seal")],
            resolution_type="replacement",
        ),
    )
    seed_analysis(
        session,
        "kb_wifi",
        make_structured(
            [
                make_issue(
                    "wifi connectivity drop",
                    resolution_summary="rebooted the router and re-paired",
                    product="Hub 2",
                )
            ],
        ),
    )
    seed_analysis(
        session,
        "kb_heating",
        make_structured(
            [
                make_issue(
                    "heating failure", resolution_status="unresolved", resolution_summary=None
                )
            ],
            resolved=False,
            resolution_type=None,
        ),
    )


@pytest.fixture
def factory(settings_on_test_db: str, migrated_engine: Any, db_session: Session) -> Any:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False)


def run_service(factory: Any, embedder: FakeEmbedder | None = None) -> Any:
    embedder = embedder or FakeEmbedder()
    return KnowledgeBaseService(factory, embedder).run(), embedder


# --- generation + persistence ------------------------------------------------------


def test_builds_documents_for_resolved_issues_only(factory: Any, db_session: Session) -> None:
    seed_knowledge_scenario(db_session)
    result, _embedder = run_service(factory)

    repo = KnowledgeDocumentRepository(db_session)
    rows = repo.all()
    assert {r.issue for r in rows} == {"base water leak", "wifi connectivity drop"}
    assert repo.count() == 2
    leak = next(r for r in rows if r.issue == "base water leak")
    assert leak.product == "Pod 5"
    assert leak.document["resolution_type"] == "replacement"
    assert "replaced the base seal" in leak.knowledge_text
    assert leak.knowledge_text.startswith("Problem:")
    assert leak.embedding_model
    assert next(iter(leak.embedding)) == 1.0  # embedded from knowledge_text ('leak' axis)

    assert result.documents == 2
    assert result.documents_embedded == 2
    assert "2" in result.summary()


def test_embeds_knowledge_text_not_json(factory: Any, db_session: Session) -> None:
    seed_knowledge_scenario(db_session)
    _result, embedder = run_service(factory)
    embedded_texts = [t for call in embedder.document_calls for t in call]
    assert all(t.startswith("Problem:") for t in embedded_texts)
    assert all("{" not in t for t in embedded_texts)


def test_rerun_is_resumable_and_does_not_reembed_unchanged(
    factory: Any, db_session: Session
) -> None:
    seed_knowledge_scenario(db_session)
    run_service(factory)

    second = FakeEmbedder()
    result, _ = run_service(factory, second)
    assert second.document_calls == []  # nothing re-embedded
    assert result.documents_embedded == 0
    assert KnowledgeDocumentRepository(db_session).count() == 2  # no duplicates


def test_changed_analysis_is_reembedded(factory: Any, db_session: Session) -> None:
    seed_knowledge_scenario(db_session)
    run_service(factory)

    # The leak conversation gets a different resolution on re-analysis.
    import uuid as uuid_mod

    upsert_analysis(
        db_session,
        uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, "kb_leak"),
        make_structured(
            [make_issue("base water leak", resolution_summary="tightened the inlet valve")],
            resolution_type="troubleshooting",
        ),
    )
    embedder = FakeEmbedder()
    result, _ = run_service(factory, embedder)
    assert result.documents_embedded == 1
    rows = KnowledgeDocumentRepository(db_session).all()
    leak = next(r for r in rows if r.issue == "base water leak")
    assert "tightened the inlet valve" in leak.knowledge_text
    assert KnowledgeDocumentRepository(db_session).count() == 2


def test_no_analyses_is_a_clean_stop(factory: Any, db_session: Session) -> None:
    result, embedder = run_service(factory)
    assert result.documents == 0
    assert embedder.document_calls == []
    assert "no analyzed conversations" in result.summary().lower()


# --- retrieval -----------------------------------------------------------------


def test_retrieval_is_semantic(factory: Any, db_session: Session) -> None:
    seed_knowledge_scenario(db_session)
    run_service(factory)

    embedder = FakeEmbedder()
    results = retrieve(db_session, embedder, "customer reports a leak under the pod", limit=2)
    assert results
    assert results[0].issue == "base water leak"
    assert results[0].knowledge_text.startswith("Problem:")
    assert embedder.query_calls == ["customer reports a leak under the pod"]


def test_retrieval_applies_metadata_filter_first(factory: Any, db_session: Session) -> None:
    seed_knowledge_scenario(db_session)
    run_service(factory)

    # Product filter narrows to Hub 2 even though the query is about leaks.
    results = retrieve(
        db_session, FakeEmbedder(), "leak", product="Hub 2", limit=5
    )
    assert [r.issue for r in results] == ["wifi connectivity drop"]


def test_retrieval_relaxes_filters_when_no_candidates(
    factory: Any, db_session: Session
) -> None:
    seed_knowledge_scenario(db_session)
    run_service(factory)

    # No documents exist for this product — the filter is progressively relaxed.
    results = retrieve(db_session, FakeEmbedder(), "leak", product="Nonexistent 9", limit=2)
    assert results
    assert results[0].issue == "base water leak"


def test_retrieval_empty_knowledge_base(factory: Any, db_session: Session) -> None:
    assert retrieve(db_session, FakeEmbedder(), "anything") == []


# --- integration: stage / CLI / API / status ------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeEmbedder]:
    from pathlib import Path

    from cxintel.config import get_settings

    embedder = FakeEmbedder()
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)  # alembic.ini for the stage
    get_settings.cache_clear()
    monkeypatch.setattr("cxintel.llm.get_embedding_provider", lambda: embedder)
    yield embedder
    get_settings.cache_clear()


def test_cli_build_kb(factory: Any, db_session: Session, cli_env: FakeEmbedder) -> None:
    from typer.testing import CliRunner

    from cxintel.cli import app as cli_app

    seed_knowledge_scenario(db_session)
    result = CliRunner().invoke(cli_app, ["build-kb"])
    assert result.exit_code == 0, result.output
    assert "2 documents" in result.output
    assert KnowledgeDocumentRepository(db_session).count() == 2


def test_cli_search(factory: Any, db_session: Session, cli_env: FakeEmbedder) -> None:
    from typer.testing import CliRunner

    from cxintel.cli import app as cli_app

    runner = CliRunner()
    empty = runner.invoke(cli_app, ["search", "leak"])
    assert empty.exit_code == 1
    assert "build-kb" in empty.output

    seed_knowledge_scenario(db_session)
    KnowledgeBaseService(factory, FakeEmbedder()).run()
    result = runner.invoke(cli_app, ["search", "leak under the pod"])
    assert result.exit_code == 0, result.output
    assert "base water leak" in result.output


def test_api_knowledge_search(
    factory: Any, db_session: Session, cli_env: FakeEmbedder
) -> None:
    from fastapi.testclient import TestClient

    from cxintel.api.app import app

    client = TestClient(app)
    assert client.get("/api/knowledge/search", params={"q": "leak"}).json() == []

    seed_knowledge_scenario(db_session)
    KnowledgeBaseService(factory, FakeEmbedder()).run()

    hits = client.get("/api/knowledge/search", params={"q": "leak under the pod"}).json()
    assert hits
    assert hits[0]["issue"] == "base water leak"
    assert hits[0]["knowledge_text"].startswith("Problem:")
    assert "distance" in hits[0]

    filtered = client.get(
        "/api/knowledge/search", params={"q": "leak", "product": "Hub 2"}
    ).json()
    assert [h["issue"] for h in filtered] == ["wifi connectivity drop"]


def test_api_knowledge_search_unconfigured_ai(
    factory: Any, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cxintel.api.app import app
    from cxintel.config import get_settings

    seed_knowledge_scenario(db_session)
    KnowledgeBaseService(factory, FakeEmbedder()).run()

    monkeypatch.setenv("GOOGLE_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = TestClient(app).get("/api/knowledge/search", params={"q": "leak"})
    finally:
        get_settings.cache_clear()
    assert response.status_code == 422
    assert "GOOGLE_API_KEY" in response.json()["detail"]


def test_status_metrics_include_embedding_count(
    factory: Any, db_session: Session, cli_env: FakeEmbedder
) -> None:
    from fastapi.testclient import TestClient

    from cxintel.api.app import app

    seed_knowledge_scenario(db_session)
    KnowledgeBaseService(factory, FakeEmbedder()).run()

    status = TestClient(app).get("/api/status").json()
    assert status["metrics"]["embedding_count"] == 2
    kb = next(s for s in status["pipeline"] if s["key"] == "knowledge_base")
    assert kb["implemented"] is True
    assert kb["complete"] is True
