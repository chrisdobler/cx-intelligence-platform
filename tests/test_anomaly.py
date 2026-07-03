"""DB-backed tests for the anomaly detection service (fake provider, no network)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cxintel.anomaly.schema import SlackAlert
from cxintel.anomaly.service import AnomalyService
from cxintel.llm import LLMExtractionError
from cxintel.models import ConversationAnalysis, ConversationIssue, IssueCatalogEntry
from cxintel.repositories import AnomalyRepository

from .test_understanding import seed_conversation


class FakeSlackProvider:
    """Returns a canned Slack alert; can be told to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.prompts: list[str] = []

    def extract(self, prompt: str, schema: type[Any], on_retry: Any = None) -> SlackAlert:
        self.prompts.append(prompt)
        if self.fail:
            raise LLMExtractionError("quota exhausted")
        return SlackAlert(text="🚨 canned alert")


def seed_issue(
    session: Session,
    external_id: str,
    day: int,
    canonical_name: str,
    *,
    severity: str = "medium",
    resolution_status: str = "resolved",
    matched: bool = True,
) -> None:
    conv_id = seed_conversation(session, external_id, day, f"text {external_id}")
    now = datetime(2026, 7, 3, tzinfo=UTC)
    # An analysis row marks the conversation as understood (stage prerequisite).
    session.merge(
        ConversationAnalysis(
            conversation_id=conv_id,
            model="fake",
            model_version="fake",
            prompt_version="1.0",
            processed_at=now,
            analysis_json={},
        )
    )
    session.add(
        ConversationIssue(
            id=uuid.uuid4(),
            conversation_id=conv_id,
            canonical_name=canonical_name,
            customer_description=f"customer says {canonical_name}",
            severity=severity,
            confidence=0.9,
            customer_impact="high",
            product="Pod 5",
            symptoms=[],
            catalog_matched=matched,
            catalog_confidence=0.9,
            resolution_status=resolution_status,
            resolution_summary=None,
            created_at=now,
        )
    )
    session.commit()


def seed_catalog(session: Session, *names: str) -> None:
    for name in names:
        session.merge(
            IssueCatalogEntry(
                canonical_name=name,
                description=name,
                first_seen_day=1,
                example_count=1,
                representative_examples=[name],
                created_at=datetime(2026, 7, 3, tzinfo=UTC),
            )
        )
    session.commit()


def seed_spike_scenario(session: Session) -> None:
    """Day 1: 4x leak. Day 2: 8x leak (spike) + 1x novel issue."""
    seed_catalog(session, "leak")
    for i in range(4):
        seed_issue(session, f"d1_{i}", 1, "leak")
    for i in range(8):
        seed_issue(session, f"d2_{i}", 2, "leak")
    seed_issue(session, "d2_novel", 2, "ghost noises", matched=False)


@pytest.fixture
def factory(settings_on_test_db: str, migrated_engine: Any, db_session: Session) -> Any:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False)


def make_service(
    factory: Any, provider: Any, tmp_path: Path, min_count: int = 5
) -> AnomalyService:
    return AnomalyService(
        factory, provider, report_path=tmp_path / "anomaly-report.md", min_count=min_count
    )


def test_detects_and_persists_anomalies(
    factory: Any, db_session: Session, tmp_path: Path
) -> None:
    seed_spike_scenario(db_session)
    provider = FakeSlackProvider()
    result = make_service(factory, provider, tmp_path).run()

    rows = AnomalyRepository(db_session).all()
    assert {(a.issue, a.day) for a in rows} == {("leak", 2), ("ghost noises", 2)}
    leak = next(a for a in rows if a.issue == "leak")
    assert "volume_spike" in leak.signals
    assert leak.metrics["baseline_count"] == 4
    assert leak.metrics["current_count"] == 8
    assert leak.delta == 100.0
    assert leak.slack_message == "🚨 canned alert"
    assert leak.recommended_action
    novel = next(a for a in rows if a.issue == "ghost noises")
    assert novel.signals == ["novel_issue"]
    assert novel.delta == 0.0
    assert result.anomalies == 2
    assert "2 anomalies" in result.summary()


def test_rerun_regenerates_without_duplicates(
    factory: Any, db_session: Session, tmp_path: Path
) -> None:
    seed_spike_scenario(db_session)
    service = make_service(factory, FakeSlackProvider(), tmp_path)
    service.run()
    service.run()
    assert AnomalyRepository(db_session).count() == 2


def test_slack_fallback_when_llm_fails(
    factory: Any, db_session: Session, tmp_path: Path
) -> None:
    seed_spike_scenario(db_session)
    result = make_service(factory, FakeSlackProvider(fail=True), tmp_path).run()
    rows = AnomalyRepository(db_session).all()
    # Deterministic fallback message — the run must not fail on alert prose.
    assert all(a.slack_message for a in rows)
    assert all("canned" not in a.slack_message for a in rows)
    assert result.anomalies == 2


def test_baseline_only_is_a_clean_stop(
    factory: Any, db_session: Session, tmp_path: Path
) -> None:
    seed_catalog(db_session, "leak")
    seed_issue(db_session, "d1_0", 1, "leak")
    result = make_service(factory, FakeSlackProvider(), tmp_path).run()
    assert result.anomalies == 0
    assert "baseline" in result.summary().lower()
    assert AnomalyRepository(db_session).count() == 0


def test_report_file_written(factory: Any, db_session: Session, tmp_path: Path) -> None:
    seed_spike_scenario(db_session)
    make_service(factory, FakeSlackProvider(), tmp_path).run()
    report = (tmp_path / "anomaly-report.md").read_text(encoding="utf-8")
    assert "# Anomaly Report" in report
    assert "leak" in report and "ghost noises" in report
    assert "volume_spike" in report
    assert "100%" in report or "100.0" in report or "100" in report


def test_webhook_delivery_when_configured(
    factory: Any, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_spike_scenario(db_session)
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: float) -> Any:
        posts.append((url, json))

        class Response:
            status_code = 200

        return Response()

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/T000/B000")
    from cxintel.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("cxintel.anomaly.service.httpx.post", fake_post)
    try:
        result = make_service(factory, FakeSlackProvider(), tmp_path).run()
    finally:
        get_settings.cache_clear()

    assert len(posts) == 2
    assert all(url == "https://hooks.slack.example/T000/B000" for url, _ in posts)
    assert all(body == {"text": "🚨 canned alert"} for _, body in posts)
    assert result.alerts_delivered == 2


def test_api_anomalies_endpoint_and_report(
    factory: Any, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cxintel.api.app import app

    client = TestClient(app)
    assert client.get("/api/anomalies").json() == []

    seed_spike_scenario(db_session)
    make_service(factory, FakeSlackProvider(), tmp_path).run()

    anomalies = client.get("/api/anomalies").json()
    assert {a["issue"] for a in anomalies} == {"leak", "ghost noises"}
    leak = next(a for a in anomalies if a["issue"] == "leak")
    assert leak["day"] == 2
    assert "volume_spike" in leak["signals"]
    assert leak["metrics"]["current_count"] == 8
    assert leak["summary"]
    assert leak["recommended_action"]
    assert leak["slack_message"] == "🚨 canned alert"

    report = client.get("/api/anomalies/report")
    assert report.status_code == 200
    assert "# Anomaly Report" in report.text
    assert "leak" in report.text


def test_cli_analyze_and_report(
    factory: Any, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from cxintel.cli import app as cli_app

    runner = CliRunner()

    # No anomalies yet → report exits 1 with a hint.
    empty = runner.invoke(cli_app, ["report"])
    assert empty.exit_code == 1
    assert "analyze" in empty.output

    seed_spike_scenario(db_session)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("ANOMALY_REPORT_PATH", str(tmp_path / "report.md"))
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)  # alembic.ini for the stage
    from cxintel.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("cxintel.llm.get_llm_provider", lambda: FakeSlackProvider())

    result = runner.invoke(cli_app, ["analyze"])
    assert result.exit_code == 0, result.output
    assert "2 anomalies" in result.output

    report = runner.invoke(cli_app, ["report"])
    assert report.exit_code == 0, report.output
    assert "leak" in report.output
    assert "ghost noises" in report.output


def test_webhook_skipped_when_unset(
    factory: Any, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_spike_scenario(db_session)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    from cxintel.config import get_settings

    get_settings.cache_clear()
    try:
        result = make_service(factory, FakeSlackProvider(), tmp_path).run()
    finally:
        get_settings.cache_clear()
    assert result.alerts_delivered == 0
    assert "skipped" in result.summary()
