"""CLI + status integration tests for ingest/stats (require local Postgres)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cxintel.cli import app

from .test_ingestion import make_record

runner = CliRunner()


def write_dataset(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "tickets.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_ingest_and_stats_roundtrip(
    tmp_path: Path, settings_on_test_db: str, monkeypatch: Any, db_session: Any
) -> None:
    path = write_dataset(
        tmp_path,
        [
            make_record("conv_0001", "resolved"),
            make_record("conv_0002", "open", False),
            make_record("conv_0003", "escalated", False),
        ],
    )
    monkeypatch.setenv("RAW_DATA_PATH", str(path))
    from cxintel.config import get_settings

    get_settings.cache_clear()

    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 0, result.output
    assert "3 conversations" in result.output
    assert "6 messages" in result.output

    # Rerun is a no-op that reports skips.
    rerun = runner.invoke(app, ["ingest"])
    assert rerun.exit_code == 0, rerun.output
    assert "3 skipped" in rerun.output

    stats = runner.invoke(app, ["stats"])
    assert stats.exit_code == 0, stats.output
    assert "3" in stats.output  # total conversations
    assert "resolved" in stats.output.lower()
    assert "escalated" in stats.output.lower()
    assert "2026-02-24" in stats.output  # date range


def test_stats_on_empty_database_exits_nonzero(settings_on_test_db: str, db_session: Any) -> None:
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 1
    assert "ingest" in result.output  # hint pointing at make ingest


def test_ingest_missing_file_fails_cleanly(
    tmp_path: Path, settings_on_test_db: str, monkeypatch: Any
) -> None:
    monkeypatch.setenv("RAW_DATA_PATH", str(tmp_path / "missing.json"))
    from cxintel.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 1
    assert "missing.json" in result.output


def test_pipeline_command_runs_remaining_and_stops_cleanly(
    tmp_path: Path, settings_on_test_db: str, monkeypatch: Any, db_session: Any
) -> None:
    path = write_dataset(tmp_path, [make_record()])
    monkeypatch.setenv("RAW_DATA_PATH", str(path))
    monkeypatch.setenv("GOOGLE_API_KEY", "")  # understand blocked deterministically
    from cxintel.config import get_settings

    get_settings.cache_clear()

    result = runner.invoke(app, ["pipeline"])
    assert result.exit_code == 0, result.output
    assert "Data Ingestion" in result.output
    assert "blocked" in result.output  # stopped at understand (AI unconfigured)


def test_runs_command_lists_cli_triggered_runs(
    tmp_path: Path, settings_on_test_db: str, monkeypatch: Any, db_session: Any
) -> None:
    path = write_dataset(tmp_path, [make_record()])
    monkeypatch.setenv("RAW_DATA_PATH", str(path))
    from cxintel.config import get_settings

    get_settings.cache_clear()
    assert runner.invoke(app, ["ingest"]).exit_code == 0

    result = runner.invoke(app, ["runs"])
    assert result.exit_code == 0, result.output
    assert "ingest" in result.output
    assert "succeeded" in result.output
    assert "cli" in result.output


def test_runs_command_empty_history(settings_on_test_db: str, db_session: Any) -> None:
    result = runner.invoke(app, ["runs"])
    assert result.exit_code == 0, result.output
    assert "No pipeline runs recorded yet." in result.output


def test_understand_without_ai_key_fails_cleanly(
    settings_on_test_db: str, monkeypatch: Any, db_session: Any
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    from cxintel.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["understand"])
    assert result.exit_code == 1
    assert "GOOGLE_API_KEY" in result.output


def test_understand_sample_and_full_flags(
    tmp_path: Path, settings_on_test_db: str, monkeypatch: Any, db_session: Any
) -> None:
    path = write_dataset(tmp_path, [make_record("conv_0001"), make_record("conv_0002", "open")])
    monkeypatch.setenv("RAW_DATA_PATH", str(path))
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    from cxintel.config import get_settings

    get_settings.cache_clear()
    assert runner.invoke(app, ["ingest"]).exit_code == 0

    class CannedProvider:
        def extract(self, prompt: str, schema: Any, on_retry: Any = None) -> Any:
            from .test_understanding_schema import FROZEN_V1_EXAMPLE

            return schema.model_validate(FROZEN_V1_EXAMPLE)

    monkeypatch.setattr("cxintel.llm.get_llm_provider", lambda **kw: CannedProvider())

    sample = runner.invoke(app, ["understand"])
    assert sample.exit_code == 0, sample.output
    assert "Analyzed 2 conversations" in sample.output

    bottlenecks = runner.invoke(app, ["bottlenecks", "--sort", "llm_seconds"])
    assert bottlenecks.exit_code == 0, bottlenecks.output
    assert "conversation" in bottlenecks.output
    assert "conv_0001" in bottlenecks.output
    assert "succeeded" in bottlenecks.output

    # Everything already analyzed — a full run just skips.
    full = runner.invoke(app, ["understand", "--full"])
    assert full.exit_code == 0, full.output
    assert "Analyzed 0 conversations" in full.output
    assert "2 already analyzed" in full.output


def test_status_reports_ingested_metrics(
    tmp_path: Path, settings_on_test_db: str, monkeypatch: Any, db_session: Any
) -> None:
    path = write_dataset(tmp_path, [make_record()])
    monkeypatch.setenv("RAW_DATA_PATH", str(path))
    from cxintel.config import get_settings

    get_settings.cache_clear()
    assert runner.invoke(app, ["ingest"]).exit_code == 0

    from cxintel.api.app import app as fastapi_app

    payload = TestClient(fastapi_app).get("/api/status").json()
    assert payload["metrics"]["imported_conversations"] == 1
    ingest_stage = next(s for s in payload["pipeline"] if s["key"] == "ingest")
    assert ingest_stage["state"] == "done"
