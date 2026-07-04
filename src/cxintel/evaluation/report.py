"""Evaluation report — the versionable Phase 7 artifact.

The canonical report is JSON (``EvaluationReport.model_dump_json``); the
markdown rendering is derived from it for humans. Regressions are detected
against the committed baseline (a previously promoted report at
``evals/baseline/evaluation-baseline.json``); the delta against the previous
``evaluation_runs`` database row is informational only.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .comparison import CaseResult
from .metrics import GroundingSuiteMetrics, RetrievalSuiteMetrics

_METRIC_TOLERANCE = 0.05


class SuiteSummary(BaseModel):
    """Pass/fail totals of one suite."""

    total: int
    passed: int
    pass_rate: float


class BaselineRef(BaseModel):
    """Where the regression baseline came from."""

    path: str
    generated_at: datetime
    dataset_version: str
    model: str


class Regression(BaseModel):
    """One detected regression against the baseline."""

    kind: str  # case | metric
    case_id: str | None = None
    detail: str


class PreviousRunDelta(BaseModel):
    """Informational comparison against the previous evaluation_runs row."""

    finished_at: datetime
    pass_rate_before: float
    pass_rate_after: float


class EvaluationReport(BaseModel):
    """The complete, deterministic result of one evaluation run."""

    run_id: uuid.UUID
    generated_at: datetime
    duration_seconds: float
    dataset_version: str
    suites_run: list[str]
    model: str
    embedding_model: str
    understanding_prompt_version: str
    resolution_prompt_version: str
    coverage: dict[str, int]
    summary: dict[str, SuiteSummary]
    retrieval_metrics: RetrievalSuiteMetrics | None = None
    grounding_metrics: GroundingSuiteMetrics | None = None
    total_tokens: int | None = None
    cases: list[CaseResult] = Field(default_factory=list)
    baseline: BaselineRef | None = None
    regressions: list[Regression] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    previous_run: PreviousRunDelta | None = None

    @property
    def total_cases(self) -> int:
        return sum(suite.total for suite in self.summary.values())

    @property
    def passed_cases(self) -> int:
        return sum(suite.passed for suite in self.summary.values())

    @property
    def pass_rate(self) -> float:
        total = self.total_cases
        return self.passed_cases / total if total else 0.0


def summarize_suite(cases: list[CaseResult]) -> SuiteSummary:
    passed = sum(1 for case in cases if case.passed)
    return SuiteSummary(
        total=len(cases),
        passed=passed,
        pass_rate=passed / len(cases) if cases else 0.0,
    )


# --- regression detection ----------------------------------------------------


def detect_regressions(
    current: EvaluationReport,
    baseline: EvaluationReport,
    *,
    metric_tolerance: float = _METRIC_TOLERANCE,
) -> tuple[list[Regression], list[str]]:
    """Regressions and improvements of ``current`` against ``baseline``.

    A case regression is a case that passed in the baseline but fails now;
    a metric regression is a suite pass rate, recall@k, or MRR that dropped by
    more than ``metric_tolerance``. Only case ids present in both reports are
    compared, so extending the dataset never manufactures regressions.
    """
    regressions: list[Regression] = []
    improvements: list[str] = []

    baseline_cases = {case.case_id: case for case in baseline.cases}
    for case in current.cases:
        before = baseline_cases.get(case.case_id)
        if before is None:
            continue
        if before.passed and not case.passed:
            failed = [check for check in case.checks if not check.passed]
            detail = (
                f"{case.case_id}: passed in baseline, now fails "
                f"({failed[0].check}: expected {failed[0].expected}, got {failed[0].actual})"
                if failed
                else f"{case.case_id}: passed in baseline, now fails"
            )
            regressions.append(Regression(kind="case", case_id=case.case_id, detail=detail))
        elif not before.passed and case.passed:
            improvements.append(case.case_id)

    for suite, current_summary in current.summary.items():
        before_summary = baseline.summary.get(suite)
        if before_summary is None:
            continue
        drop = before_summary.pass_rate - current_summary.pass_rate
        if drop > metric_tolerance:
            regressions.append(
                Regression(
                    kind="metric",
                    detail=(
                        f"{suite} pass rate dropped "
                        f"{before_summary.pass_rate:.2f} → {current_summary.pass_rate:.2f}"
                    ),
                )
            )

    metric_pairs: list[tuple[str, float | None, float | None]] = []
    if current.retrieval_metrics and baseline.retrieval_metrics:
        metric_pairs.extend(
            [
                (
                    "recall@k",
                    baseline.retrieval_metrics.recall_at_k,
                    current.retrieval_metrics.recall_at_k,
                ),
                ("MRR", baseline.retrieval_metrics.mrr, current.retrieval_metrics.mrr),
            ]
        )
    for name, before_value, after_value in metric_pairs:
        if before_value is None or after_value is None:
            continue
        if before_value - after_value > metric_tolerance:
            regressions.append(
                Regression(
                    kind="metric",
                    detail=f"{name} dropped {before_value:.2f} → {after_value:.2f}",
                )
            )

    return regressions, improvements


# --- baseline files ----------------------------------------------------------


def load_baseline(path: Path) -> EvaluationReport | None:
    """The committed baseline report, or None when none has been promoted."""
    if not path.exists():
        return None
    return EvaluationReport.model_validate_json(path.read_text())


def promote_baseline(report_json_path: Path, baseline_path: Path) -> None:
    """Promote the current report to the committed regression baseline."""
    if not report_json_path.exists():
        raise FileNotFoundError(
            f"No evaluation report at {report_json_path} — run 'app evaluate' first."
        )
    # Validate before promoting: a corrupt baseline would poison every future run.
    EvaluationReport.model_validate_json(report_json_path.read_text())
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(report_json_path, baseline_path)


# --- markdown rendering ------------------------------------------------------


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_markdown(report: EvaluationReport) -> str:
    """Human-readable rendering of the canonical JSON report."""
    lines = [
        "# Evaluation Report",
        "",
        f"- Generated: {report.generated_at.isoformat()}",
        f"- Duration: {report.duration_seconds:.1f}s",
        f"- Dataset: v{report.dataset_version}",
        f"- Model: {report.model} (embeddings: {report.embedding_model})",
        (
            f"- Prompt versions: understanding {report.understanding_prompt_version}, "
            f"resolution {report.resolution_prompt_version}"
        ),
    ]
    if report.total_tokens is not None:
        lines.append(f"- Tokens used: {report.total_tokens:,}")
    lines += [
        "",
        "## Summary",
        "",
        "| Suite | Cases | Passed | Pass rate |",
        "| --- | --- | --- | --- |",
    ]
    for suite, summary in report.summary.items():
        lines.append(
            f"| {suite} | {summary.total} | {summary.passed} | {_percent(summary.pass_rate)} |"
        )
    lines += [
        "",
        f"**Overall: {report.passed_cases}/{report.total_cases} ({_percent(report.pass_rate)})**",
    ]

    if report.baseline is None:
        lines.append("")
        lines.append("_No baseline promoted yet — regressions not evaluated._")
    else:
        lines += [
            "",
            (
                f"Regressions vs baseline ({report.baseline.generated_at.date()}, "
                f"dataset v{report.baseline.dataset_version}): {len(report.regressions)}"
            ),
        ]
        if report.regressions:
            lines.append("")
            lines.append("## Regressions")
            lines.append("")
            lines.extend(f"- {regression.detail}" for regression in report.regressions)
        if report.improvements:
            lines.append("")
            lines.append(f"Newly passing: {', '.join(report.improvements)}")

    if report.previous_run is not None:
        lines += [
            "",
            (
                f"Previous run ({report.previous_run.finished_at.date()}): pass rate "
                f"{_percent(report.previous_run.pass_rate_before)} → "
                f"{_percent(report.previous_run.pass_rate_after)} (informational)"
            ),
        ]

    if report.retrieval_metrics is not None:
        m = report.retrieval_metrics
        lines += [
            "",
            "## Retrieval metrics",
            "",
            (
                f"recall@k {m.recall_at_k:.2f} | precision@k {m.precision_at_k:.2f} | "
                f"hit@k {m.hit_at_k:.2f} | MRR {m.mrr:.2f} | "
                f"filter relaxed {_percent(m.filter_relaxed_rate)}"
            ),
        ]
    if report.grounding_metrics is not None:
        g = report.grounding_metrics
        lines += [
            "",
            "## Grounding metrics",
            "",
            (
                f"citation validity {_percent(g.citation_validity_rate)} | "
                f"grounded accuracy {_percent(g.grounded_accuracy)} | "
                f"evidence strength match {_percent(g.evidence_strength_match_rate)} | "
                f"downgrades {_percent(g.downgrade_rate)}"
            ),
        ]

    failed_cases = [case for case in report.cases if not case.passed]
    if failed_cases:
        lines += ["", "## Failed checks", ""]
        for case in failed_cases:
            lines.append(f"### {case.case_id} — FAIL")
            if case.error:
                lines.append(f"- execution error: {case.error}")
            lines.extend(
                f"- {check.check}: expected {check.expected}, got {check.actual}"
                for check in case.checks
                if not check.passed
            )
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
