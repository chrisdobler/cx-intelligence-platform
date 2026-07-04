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
    _seed_history_run(session)
    session.commit()


def _seed_history_run(
    session: Session,
    *,
    finished_at: datetime = datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    pass_rate: float = 0.75,
    passed_cases: int = 3,
    understanding_prompt_version: str = "1.2",
    resolution_prompt_version: str = "1.0",
    model: str = "gemini-2.5-flash",
    suite_rates: dict[str, float] | None = None,
    recall_at_k: float = 1.0,
    mrr: float = 1.0,
    total_tokens: int = 1234,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    report = _report_dict(run_id)
    summary = suite_rates or {"understanding": 0.5, "retrieval": 1.0, "resolution": 1.0}
    report.update(
        {
            "generated_at": finished_at.isoformat(),
            "model": model,
            "understanding_prompt_version": understanding_prompt_version,
            "resolution_prompt_version": resolution_prompt_version,
            "summary": {
                suite: {
                    "total": 1,
                    "passed": 1 if rate >= 1.0 else 0,
                    "pass_rate": rate,
                }
                for suite, rate in summary.items()
            },
            "retrieval_metrics": {
                "cases": 1,
                "recall_at_k": recall_at_k,
                "precision_at_k": 1.0,
                "hit_at_k": 1.0,
                "mrr": mrr,
                "filter_relaxed_rate": 0.0,
            },
            "total_tokens": total_tokens,
        }
    )
    session.add(
        EvaluationRun(
            id=run_id,
            pipeline_run_id=None,
            dataset_version="1.0",
            model=model,
            embedding_model="gemini-embedding-001",
            understanding_prompt_version=understanding_prompt_version,
            resolution_prompt_version=resolution_prompt_version,
            suites=["understanding", "retrieval", "resolution"],
            status="succeeded",
            total_cases=4,
            passed_cases=passed_cases,
            pass_rate=pass_rate,
            regression_count=1,
            retrieval_metrics={"recall_at_k": recall_at_k, "mrr": mrr},
            grounding_metrics=None,
            total_tokens=total_tokens,
            report=report,
            started_at=finished_at,
            finished_at=finished_at,
            duration_seconds=42.0,
            error=None,
        )
    )
    return run_id


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


def test_history_is_empty_without_any_run(
    settings_on_test_db: str, db_session: Session
) -> None:
    client = TestClient(app)
    payload = client.get("/api/evaluation/history").json()
    assert payload == {
        "available": False,
        "runs": [],
        "current": None,
        "previous": None,
        "best": None,
        "trend_delta": None,
        "comparisons": [],
    }


def test_history_returns_single_current_run_without_trend(
    settings_on_test_db: str, db_session: Session
) -> None:
    run_id = _seed_history_run(db_session, pass_rate=0.944, passed_cases=4)
    db_session.commit()

    client = TestClient(app)
    payload = client.get("/api/evaluation/history").json()

    assert payload["available"] is True
    assert len(payload["runs"]) == 1
    assert payload["current"]["id"] == str(run_id)
    assert payload["previous"] is None
    assert payload["best"]["id"] == str(run_id)
    assert payload["trend_delta"] is None
    assert payload["comparisons"][0] == {
        "key": "overall",
        "label": "Overall",
        "current": 0.944,
        "previous": None,
        "delta": None,
    }


def test_history_compares_recent_runs_chronologically(
    settings_on_test_db: str, db_session: Session
) -> None:
    oldest = _seed_history_run(
        db_session,
        finished_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
        pass_rate=0.886,
        passed_cases=3,
        understanding_prompt_version="1.1",
        resolution_prompt_version="0.9",
        suite_rates={"understanding": 0.75, "retrieval": 1.0, "resolution": 0.67},
        recall_at_k=0.78,
        mrr=0.64,
        total_tokens=900,
    )
    previous = _seed_history_run(
        db_session,
        finished_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        pass_rate=0.921,
        passed_cases=3,
        understanding_prompt_version="1.2",
        resolution_prompt_version="0.9",
        suite_rates={"understanding": 1.0, "retrieval": 1.0, "resolution": 0.67},
        recall_at_k=0.8,
        mrr=0.7,
        total_tokens=950,
    )
    current = _seed_history_run(
        db_session,
        finished_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        pass_rate=0.944,
        passed_cases=4,
        understanding_prompt_version="1.2",
        resolution_prompt_version="1.0",
        model="gemini-2.5-flash-lite",
        suite_rates={"understanding": 1.0, "retrieval": 1.0, "resolution": 0.833},
        recall_at_k=0.84,
        mrr=0.73,
        total_tokens=1010,
    )
    db_session.commit()

    client = TestClient(app)
    payload = client.get("/api/evaluation/history?limit=20").json()

    assert [run["id"] for run in payload["runs"]] == [str(oldest), str(previous), str(current)]
    assert payload["current"]["id"] == str(current)
    assert payload["previous"]["id"] == str(previous)
    assert payload["best"]["id"] == str(current)
    assert payload["trend_delta"] == pytest.approx(0.023)
    assert payload["current"]["model"] == "gemini-2.5-flash-lite"
    assert payload["current"]["understanding_prompt_version"] == "1.2"
    assert payload["current"]["resolution_prompt_version"] == "1.0"
    assert payload["current"]["retrieval_metrics"] == {"recall_at_k": 0.84, "mrr": 0.73}
    assert payload["current"]["total_tokens"] == 1010
    comparisons = {item["key"]: item for item in payload["comparisons"]}
    assert comparisons["overall"]["delta"] == pytest.approx(0.023)
    assert comparisons["retrieval"]["delta"] == pytest.approx(0.0)
    assert comparisons["understanding"]["delta"] == pytest.approx(0.0)
    assert comparisons["resolution"]["delta"] == pytest.approx(0.163)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    from cxintel.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
