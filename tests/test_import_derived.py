"""Import of pre-generated AI-derived dataset snapshots."""

from __future__ import annotations

import csv
import io
import json
import tarfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from cxintel.api.app import app as fastapi_app
from cxintel.cli import app as cli_app
from cxintel.pipeline.import_derived import SNAPSHOT_FORMAT, import_derived_data
from cxintel.repositories import (
    AnomalyRepository,
    ConversationAnalysisRepository,
    ConversationIssueRepository,
    ConversationRepository,
    IssueCatalogRepository,
    KnowledgeDocumentRepository,
    MessageRepository,
    PipelineRunRepository,
)

from .test_pipeline_reset import _seed_derived_artifacts
from .test_understanding import seed_conversation

runner = CliRunner()

_COLUMNS: dict[str, list[str]] = {
    "conversation_analyses": [
        "conversation_id",
        "model",
        "model_version",
        "prompt_version",
        "processed_at",
        "analysis_json",
    ],
    "conversation_issues": [
        "id",
        "conversation_id",
        "canonical_name",
        "customer_description",
        "severity",
        "confidence",
        "customer_impact",
        "product",
        "symptoms",
        "catalog_matched",
        "catalog_confidence",
        "resolution_status",
        "resolution_summary",
        "created_at",
    ],
    "issue_catalog": [
        "canonical_name",
        "description",
        "first_seen_day",
        "example_count",
        "representative_examples",
        "created_at",
    ],
    "anomalies": [
        "id",
        "day",
        "observation_date",
        "baseline_date",
        "issue",
        "severity",
        "delta",
        "description",
        "slack_message",
        "signals",
        "metrics",
        "recommended_action",
        "created_at",
    ],
    "knowledge_documents": [
        "id",
        "conversation_id",
        "issue",
        "product",
        "document",
        "knowledge_text",
        "embedding",
        "embedding_model",
        "created_at",
    ],
}


def _csv_text(table: str, rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_COLUMNS[table], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _write_snapshot(
    path: Path,
    conversation_id: uuid.UUID,
    *,
    embedding_dim: int = 3072,
    omit_table: str | None = None,
) -> None:
    now = datetime(2026, 7, 4, tzinfo=UTC).isoformat()
    tables: dict[str, list[dict[str, Any]]] = {
        "conversation_analyses": [
            {
                "conversation_id": str(conversation_id),
                "model": "cached",
                "model_version": "cached-v1",
                "prompt_version": "understanding-v1",
                "processed_at": now,
                "analysis_json": json.dumps({"summary": "cached analysis"}),
            }
        ],
        "conversation_issues": [
            {
                "id": str(uuid.uuid4()),
                "conversation_id": str(conversation_id),
                "canonical_name": "cached leak",
                "customer_description": "water under pod",
                "severity": "high",
                "confidence": "0.92",
                "customer_impact": "floor damage",
                "product": "Pod 5",
                "symptoms": json.dumps(["water", "floor"]),
                "catalog_matched": "true",
                "catalog_confidence": "0.88",
                "resolution_status": "resolved",
                "resolution_summary": "replaced seal",
                "created_at": now,
            }
        ],
        "issue_catalog": [
            {
                "canonical_name": "cached leak",
                "description": "Water leaking from pod",
                "first_seen_day": "1",
                "example_count": "1",
                "representative_examples": json.dumps(["water under pod"]),
                "created_at": now,
            }
        ],
        "anomalies": [
            {
                "id": str(uuid.uuid4()),
                "day": "2",
                "observation_date": "2026-02-26 12:00:00+00:00",
                "baseline_date": "2026-02-25 12:00:00+00:00",
                "issue": "cached leak",
                "severity": "high",
                "delta": "75.0",
                "description": "Cached leak spike",
                "slack_message": "Cached alert",
                "signals": json.dumps(["volume_spike"]),
                "metrics": json.dumps({"baseline_count": 1, "current_count": 3}),
                "recommended_action": "Inspect seals",
                "created_at": now,
            }
        ],
        "knowledge_documents": [
            {
                "id": str(uuid.uuid4()),
                "conversation_id": str(conversation_id),
                "issue": "cached leak",
                "product": "Pod 5",
                "document": json.dumps({"issue": "cached leak", "product": "Pod 5"}),
                "knowledge_text": "Problem\nCached leak\nResolution\nReplace seal",
                "embedding": "[" + ",".join(["0"] * embedding_dim) + "]",
                "embedding_model": "gemini-embedding-001",
                "created_at": now,
            }
        ],
    }
    if omit_table is not None:
        tables.pop(omit_table)
    manifest = {
        "format": SNAPSHOT_FORMAT,
        "tables": {
            table: {"path": f"tables/{table}.csv", "rows": len(rows)}
            for table, rows in tables.items()
        },
    }
    with zipfile.ZipFile(path, "w") as snapshot:
        snapshot.writestr("manifest.json", json.dumps(manifest))
        for table, rows in tables.items():
            snapshot.writestr(f"tables/{table}.csv", _csv_text(table, rows))


def _derived_counts(session: Session) -> tuple[int, int, int, int, int]:
    return (
        ConversationAnalysisRepository(session).count(),
        ConversationIssueRepository(session).count(),
        IssueCatalogRepository(session).count(),
        AnomalyRepository(session).count(),
        KnowledgeDocumentRepository(session).count(),
    )


def test_import_derived_data_restores_only_derived_tables(
    tmp_path: Path, settings_on_test_db: str, db_session: Session
) -> None:
    conversation_id = seed_conversation(db_session, "conv_cached", 1, "cached marker")
    db_session.rollback()
    snapshot = tmp_path / "derived.zip"
    _write_snapshot(snapshot, conversation_id)

    summary = import_derived_data(snapshot, trigger="cli")
    db_session.expire_all()

    assert "Imported pre-generated AI dataset" in summary
    assert ConversationRepository(db_session).count() == 1
    assert MessageRepository(db_session).count() == 1
    assert _derived_counts(db_session) == (1, 1, 1, 1, 1)
    analysis = ConversationAnalysisRepository(db_session).get(conversation_id)
    assert analysis is not None
    assert analysis.analysis_json == {"summary": "cached analysis"}
    run = PipelineRunRepository(db_session).recent(limit=1)[0]
    assert run.stage_key == "import_derived"
    assert run.status == "succeeded"
    assert run.trigger == "cli"


def test_import_derived_data_restores_from_data_artifacts_bundle(
    tmp_path: Path, settings_on_test_db: str, db_session: Session
) -> None:
    conversation_id = seed_conversation(db_session, "conv_bundle_cached", 1, "cached marker")
    db_session.rollback()
    snapshot = tmp_path / "derived-ai-dataset.zip"
    _write_snapshot(snapshot, conversation_id)
    bundle = tmp_path / "data-artifacts.tgz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(snapshot, arcname="derived-ai-dataset.zip")

    summary = import_derived_data(bundle, trigger="cli")
    db_session.expire_all()

    assert "Imported pre-generated AI dataset" in summary
    assert _derived_counts(db_session) == (1, 1, 1, 1, 1)


def test_import_derived_data_rolls_back_on_bad_snapshot(
    tmp_path: Path, settings_on_test_db: str, db_session: Session
) -> None:
    preserved_conversation_id = _seed_derived_artifacts(db_session)
    db_session.rollback()
    snapshot = tmp_path / "bad-derived.zip"
    _write_snapshot(snapshot, preserved_conversation_id, embedding_dim=3)

    try:
        import_derived_data(snapshot, trigger="cli")
    except Exception as exc:
        assert "expected 3072 dimensions" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("bad snapshot unexpectedly imported")
    db_session.expire_all()

    assert _derived_counts(db_session)[:4] == (1, 1, 1, 1)
    assert ConversationAnalysisRepository(db_session).get(preserved_conversation_id) is not None
    run = PipelineRunRepository(db_session).recent(limit=1)[0]
    assert run.stage_key == "import_derived"
    assert run.status == "failed"


def test_import_derived_data_rejects_missing_tables(
    tmp_path: Path, settings_on_test_db: str, db_session: Session
) -> None:
    conversation_id = seed_conversation(db_session, "conv_missing_table", 1, "cached marker")
    db_session.rollback()
    snapshot = tmp_path / "missing-table.zip"
    _write_snapshot(snapshot, conversation_id, omit_table="anomalies")

    try:
        import_derived_data(snapshot, trigger="cli")
    except Exception as exc:
        assert "manifest is missing table(s): anomalies" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("invalid snapshot unexpectedly imported")


def test_import_derived_endpoint_returns_job_and_fresh_status(
    tmp_path: Path,
    settings_on_test_db: str,
    monkeypatch: Any,
    db_session: Session,
) -> None:
    from cxintel.api import app as api_app
    from cxintel.api import status as api_status
    from cxintel.pipeline.jobs import JobTracker

    tracker = JobTracker()
    monkeypatch.setattr(tracker, "_spawn", lambda fn: fn())
    monkeypatch.setattr(api_app, "TRACKER", tracker)
    monkeypatch.setattr(api_status, "TRACKER", tracker)
    conversation_id = seed_conversation(db_session, "conv_api_cached", 1, "cached marker")
    db_session.rollback()
    snapshot = tmp_path / "derived.zip"
    _write_snapshot(snapshot, conversation_id)
    monkeypatch.setenv("DERIVED_DATA_PATH", str(snapshot))
    from cxintel.config import get_settings

    get_settings.cache_clear()
    client = TestClient(fastapi_app)

    response = client.post("/api/pipeline/import-derived")
    assert response.status_code == 202, response.text
    assert response.json()["target"] == "import_derived"

    payload = client.get("/api/status").json()
    assert payload["derived_import"]["exists"] is True
    assert payload["metrics"]["processed_conversations"] == 1
    assert payload["metrics"]["embedding_count"] == 1
    assert client.get("/api/pipeline/runs").json()[0]["stage_label"] == (
        "Import Pre-generated AI Dataset"
    )


def test_import_derived_cli_command(
    tmp_path: Path, settings_on_test_db: str, db_session: Session
) -> None:
    conversation_id = seed_conversation(db_session, "conv_cli_cached", 1, "cached marker")
    db_session.rollback()
    snapshot = tmp_path / "derived.zip"
    _write_snapshot(snapshot, conversation_id)

    result = runner.invoke(cli_app, ["import-derived", str(snapshot)])

    assert result.exit_code == 0, result.output
    assert "Imported pre-generated AI dataset" in result.output


def test_static_control_center_exposes_import_derived_action() -> None:
    html = Path("src/cxintel/api/static/index.html").read_text(encoding="utf-8")

    assert "No AI-generated data is currently available." in html
    assert "Import Pre-generated AI Dataset" in html
    assert "/api/pipeline/import-derived" in html
    assert 'opt.value !== "retry_failures"' in html


def test_static_control_center_collapses_extra_anomalies() -> None:
    html = Path("src/cxintel/api/static/index.html").read_text(encoding="utf-8")

    assert "const ANOMALY_INITIAL_VISIBLE = 2;" in html
    assert "visibleAnomalies.map(anomalyCardHTML)" in html
    assert "ordered.slice(0, ANOMALY_INITIAL_VISIBLE)" in html
    assert 'data-anomaly-more="1"' in html
    assert "Show fewer" in html
    assert "Show ${hiddenCount} more" in html
