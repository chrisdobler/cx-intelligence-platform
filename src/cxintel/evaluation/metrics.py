"""Suite-level metric aggregation — pure functions over per-case metrics.

Per-case numbers are produced by :mod:`.comparison`; this module only
averages them, so every aggregate is trivially reproducible from the report's
case list.
"""

from __future__ import annotations

from pydantic import BaseModel

from .comparison import CaseResult


class RetrievalSuiteMetrics(BaseModel):
    """Aggregated retrieval quality over the retrieval suite."""

    cases: int
    recall_at_k: float
    precision_at_k: float
    hit_at_k: float
    mrr: float
    filter_relaxed_rate: float


class GroundingSuiteMetrics(BaseModel):
    """Aggregated grounding quality over the resolution suite."""

    cases: int
    citation_validity_rate: float
    grounded_accuracy: float
    evidence_strength_match_rate: float
    downgrade_rate: float


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _mean_metric(cases: list[CaseResult], key: str) -> float:
    return _mean([case.metrics[key] for case in cases if key in case.metrics])


def aggregate_retrieval(cases: list[CaseResult]) -> RetrievalSuiteMetrics | None:
    """Mean retrieval metrics over executed retrieval cases (None when none ran)."""
    if not cases:
        return None
    return RetrievalSuiteMetrics(
        cases=len(cases),
        recall_at_k=_mean_metric(cases, "recall"),
        precision_at_k=_mean_metric(cases, "precision"),
        hit_at_k=_mean_metric(cases, "hit"),
        mrr=_mean_metric(cases, "mrr"),
        filter_relaxed_rate=_mean_metric(cases, "filter_relaxed"),
    )


def aggregate_grounding(cases: list[CaseResult]) -> GroundingSuiteMetrics | None:
    """Mean grounding metrics over executed resolution cases (None when none ran)."""
    if not cases:
        return None
    return GroundingSuiteMetrics(
        cases=len(cases),
        citation_validity_rate=_mean_metric(cases, "raw_citations_valid"),
        grounded_accuracy=_mean_metric(cases, "grounded_expected_match"),
        evidence_strength_match_rate=_mean_metric(cases, "evidence_strength_match"),
        downgrade_rate=_mean_metric(cases, "downgraded"),
    )
