"""Evaluation report — markdown rendering, regression detection, baseline files."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cxintel.evaluation.comparison import CaseResult, CheckResult
from cxintel.evaluation.metrics import RetrievalSuiteMetrics
from cxintel.evaluation.report import (
    EvaluationReport,
    detect_regressions,
    load_baseline,
    promote_baseline,
    render_markdown,
    summarize_suite,
)


def _case(case_id: str, suite: str, passed: bool) -> CaseResult:
    checks = [
        CheckResult(
            check="resolution.resolved",
            kind="exact",
            expected="True",
            actual=str(passed),
            passed=passed,
        )
    ]
    return CaseResult(case_id=case_id, suite=suite, passed=passed, checks=checks)


def _report(cases: list[CaseResult], **overrides: Any) -> EvaluationReport:
    suites = sorted({case.suite for case in cases})
    defaults: dict[str, Any] = {
        "run_id": uuid.uuid4(),
        "generated_at": datetime(2026, 7, 4, tzinfo=UTC),
        "duration_seconds": 12.5,
        "dataset_version": "1.0",
        "suites_run": suites,
        "model": "gemini-2.5-flash",
        "embedding_model": "gemini-embedding-001",
        "understanding_prompt_version": "1.2",
        "resolution_prompt_version": "1.0",
        "coverage": {suite: len([c for c in cases if c.suite == suite]) for suite in suites},
        "summary": {
            suite: summarize_suite([c for c in cases if c.suite == suite]) for suite in suites
        },
        "cases": cases,
    }
    defaults.update(overrides)
    return EvaluationReport(**defaults)


def test_summarize_suite() -> None:
    summary = summarize_suite([_case("a", "retrieval", True), _case("b", "retrieval", False)])
    assert (summary.total, summary.passed, summary.pass_rate) == (2, 1, 0.5)
    empty = summarize_suite([])
    assert (empty.total, empty.passed, empty.pass_rate) == (0, 0, 0.0)


def test_case_regression_and_improvement_detection() -> None:
    baseline = _report([_case("a", "understanding", True), _case("b", "understanding", False)])
    current = _report([_case("a", "understanding", False), _case("b", "understanding", True)])
    regressions, improvements = detect_regressions(current, baseline)
    case_regressions = [r for r in regressions if r.kind == "case"]
    assert [r.case_id for r in case_regressions] == ["a"]
    assert "expected True, got False" in case_regressions[0].detail
    assert improvements == ["b"]


def test_disjoint_case_sets_produce_no_case_regressions() -> None:
    baseline = _report([_case("old-case", "retrieval", True)])
    current = _report([_case("new-case", "retrieval", False)])
    regressions, improvements = detect_regressions(current, baseline)
    assert [r for r in regressions if r.kind == "case"] == []
    assert improvements == []


def test_suite_pass_rate_drop_beyond_tolerance_is_a_regression() -> None:
    baseline = _report([_case(f"c{i}", "understanding", True) for i in range(10)])
    current = _report(
        [_case(f"c{i}", "understanding", i >= 2) for i in range(10)]  # 80% (drop 0.2)
    )
    regressions, _ = detect_regressions(current, baseline)
    metric_regressions = [r for r in regressions if r.kind == "metric"]
    assert any("understanding pass rate dropped" in r.detail for r in metric_regressions)


def test_metric_drop_within_tolerance_is_not_a_regression() -> None:
    baseline = _report(
        [_case("r1", "retrieval", True)],
        retrieval_metrics=RetrievalSuiteMetrics(
            cases=1, recall_at_k=0.90, precision_at_k=1.0, hit_at_k=1.0, mrr=0.9,
            filter_relaxed_rate=0.0,
        ),
    )
    current = _report(
        [_case("r1", "retrieval", True)],
        retrieval_metrics=RetrievalSuiteMetrics(
            cases=1, recall_at_k=0.87, precision_at_k=1.0, hit_at_k=1.0, mrr=0.88,
            filter_relaxed_rate=0.0,
        ),
    )
    regressions, _ = detect_regressions(current, baseline)
    assert regressions == []


def test_retrieval_metric_drop_beyond_tolerance_is_a_regression() -> None:
    baseline = _report(
        [_case("r1", "retrieval", True)],
        retrieval_metrics=RetrievalSuiteMetrics(
            cases=1, recall_at_k=0.9, precision_at_k=1.0, hit_at_k=1.0, mrr=0.9,
            filter_relaxed_rate=0.0,
        ),
    )
    current = _report(
        [_case("r1", "retrieval", True)],
        retrieval_metrics=RetrievalSuiteMetrics(
            cases=1, recall_at_k=0.6, precision_at_k=1.0, hit_at_k=1.0, mrr=0.9,
            filter_relaxed_rate=0.0,
        ),
    )
    regressions, _ = detect_regressions(current, baseline)
    assert any("recall@k dropped" in r.detail for r in regressions)


def test_markdown_rendering_covers_headline_and_failures() -> None:
    report = _report(
        [_case("good", "understanding", True), _case("bad", "resolution", False)],
        total_tokens=41230,
    )
    markdown = render_markdown(report)
    assert "# Evaluation Report" in markdown
    assert "gemini-2.5-flash" in markdown
    assert "understanding 1.2, resolution 1.0" in markdown
    assert "41,230" in markdown
    assert "No baseline promoted yet" in markdown
    assert "### bad — FAIL" in markdown
    assert "good" not in markdown.split("## Failed checks")[1]


def test_baseline_round_trip_and_promotion(tmp_path: Path) -> None:
    report = _report([_case("a", "understanding", True)])
    report_path = tmp_path / "evaluation-report.json"
    baseline_path = tmp_path / "baseline" / "evaluation-baseline.json"
    assert load_baseline(baseline_path) is None

    report_path.write_text(report.model_dump_json())
    promote_baseline(report_path, baseline_path)
    loaded = load_baseline(baseline_path)
    assert loaded is not None and loaded.run_id == report.run_id


def test_promote_baseline_requires_a_report(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run 'app evaluate' first"):
        promote_baseline(tmp_path / "missing.json", tmp_path / "baseline.json")


def test_promote_baseline_rejects_corrupt_report(tmp_path: Path) -> None:
    report_path = tmp_path / "evaluation-report.json"
    report_path.write_text('{"not": "a report"}')
    with pytest.raises(ValidationError):
        promote_baseline(report_path, tmp_path / "baseline.json")
