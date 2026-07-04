"""API tests for the evaluation endpoints (empty and populated)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cxintel.api.app import app
from cxintel.models import EvaluationRun


def _report_dict(run_id: uuid.UUID) -> dict[str, Any]:
    return {
        "run_id": str(run_id),
        "generated_at": "2026-07-04T12:00:00Z",
        "duration_seconds": 42.0,
        "dataset_version": "1.0",
        "suites_run": ["understanding", "retrieval", "resolution"],
        "model": "gemini-2.5-flash",
        "embedding_model": "gemini-embedding-001",
        "understanding_prompt_version": "1.2",
        "resolution_prompt_version": "1.0",
        "coverage": {"understanding": 2, "retrieval": 1, "resolution": 1},
        "summary": {
            "understanding": {"total": 2, "passed": 1, "pass_rate": 0.5},
            "retrieval": {"total": 1, "passed": 1, "pass_rate": 1.0},
            "resolution": {"total": 1, "passed": 1, "pass_rate": 1.0},
        },
        "retrieval_metrics": {
            "cases": 1,
            "recall_at_k": 1.0,
            "precision_at_k": 1.0,
            "hit_at_k": 1.0,
            "mrr": 1.0,
            "filter_relaxed_rate": 0.0,
        },
        "grounding_metrics": None,
        "total_tokens": 1234,
        "cases": [
            {"case_id": "u1", "suite": "understanding", "passed": True, "checks": []},
            {
                "case_id": "u2",
                "suite": "understanding",
                "passed": False,
                "checks": [
                    {
                        "check": "resolution.resolved",
                        "kind": "exact",
                        "expected": "True",
                        "actual": "False",
                        "passed": False,
                    }
                ],
            },
            {"case_id": "r1", "suite": "retrieval", "passed": True, "checks": []},
            {"case_id": "s1", "suite": "resolution", "passed": True, "checks": []},
        ],
        "baseline": {
            "path": "evals/baseline/evaluation-baseline.json",
            "generated_at": "2026-07-01T12:00:00Z",
            "dataset_version": "1.0",
            "model": "gemini-2.5-flash",
        },
        "regressions": [
            {"kind": "case", "case_id": "u2", "detail": "u2: passed in baseline, now fails"}
        ],
        "improvements": [],
        "previous_run": None,
    }


def _seed_run(session: Session) -> None:
    run_id = uuid.uuid4()
    now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    session.add(
        EvaluationRun(
            id=run_id,
            pipeline_run_id=None,
            dataset_version="1.0",
            model="gemini-2.5-flash",
            embedding_model="gemini-embedding-001",
            understanding_prompt_version="1.2",
            resolution_prompt_version="1.0",
            suites=["understanding", "retrieval", "resolution"],
            status="succeeded",
            total_cases=4,
            passed_cases=3,
            pass_rate=0.75,
            regression_count=1,
            retrieval_metrics={"recall_at_k": 1.0},
            grounding_metrics=None,
            total_tokens=1234,
            report=_report_dict(run_id),
            started_at=now,
            finished_at=now,
            duration_seconds=42.0,
            error=None,
        )
    )
    session.commit()


def test_latest_is_unavailable_without_any_run(
    settings_on_test_db: str, db_session: Session
) -> None:
    client = TestClient(app)
    payload = client.get("/api/evaluation/latest").json()
    assert payload["available"] is False


def test_latest_returns_headline_numbers(settings_on_test_db: str, db_session: Session) -> None:
    _seed_run(db_session)
    client = TestClient(app)
    payload = client.get("/api/evaluation/latest").json()
    assert payload["available"] is True
    assert payload["pass_rate"] == 0.75
    assert payload["total_cases"] == 4 and payload["passed_cases"] == 3
    assert payload["model"] == "gemini-2.5-flash"
    assert payload["understanding_prompt_version"] == "1.2"
    assert payload["resolution_prompt_version"] == "1.0"
    assert payload["baseline_available"] is True
    assert payload["regression_count"] == 1
    assert payload["regressions"] == ["u2: passed in baseline, now fails"]
    assert {s["suite"] for s in payload["suites"]} == {"understanding", "retrieval", "resolution"}
    assert payload["failed_case_ids"] == ["u2"]
    assert payload["total_tokens"] == 1234


def test_report_endpoint_renders_markdown(settings_on_test_db: str, db_session: Session) -> None:
    client = TestClient(app)
    empty = client.get("/api/evaluation/report")
    assert empty.status_code == 200
    assert "No evaluation has run yet" in empty.text

    _seed_run(db_session)
    populated = client.get("/api/evaluation/report")
    assert populated.status_code == 200
    assert "# Evaluation Report" in populated.text
    assert "u2" in populated.text


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    from cxintel.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
