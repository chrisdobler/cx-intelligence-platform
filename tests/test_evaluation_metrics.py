"""Suite metric aggregation — plain arithmetic over per-case metrics."""

from __future__ import annotations

from cxintel.evaluation.comparison import CaseResult
from cxintel.evaluation.metrics import aggregate_grounding, aggregate_retrieval


def _case(suite: str, metrics: dict[str, float], case_id: str = "c") -> CaseResult:
    return CaseResult(case_id=case_id, suite=suite, passed=True, metrics=metrics)


def test_aggregate_retrieval_means() -> None:
    cases = [
        _case("retrieval", {"recall": 1.0, "hit": 1.0, "mrr": 1.0, "filter_relaxed": 0.0}, "r1"),
        _case("retrieval", {"recall": 0.5, "hit": 1.0, "mrr": 0.25, "filter_relaxed": 1.0}, "r2"),
    ]
    metrics = aggregate_retrieval(cases)
    assert metrics is not None
    assert metrics.cases == 2
    assert metrics.recall_at_k == 0.75
    assert metrics.hit_at_k == 1.0
    assert metrics.mrr == 0.625
    assert metrics.filter_relaxed_rate == 0.5


def test_aggregate_retrieval_empty_is_none() -> None:
    assert aggregate_retrieval([]) is None
    assert aggregate_grounding([]) is None


def test_aggregate_grounding_means() -> None:
    cases = [
        _case(
            "resolution",
            {
                "raw_citations_valid": 1.0,
                "grounded_expected_match": 1.0,
                "evidence_strength_match": 1.0,
                "downgraded": 0.0,
            },
            "g1",
        ),
        _case(
            "resolution",
            {
                "raw_citations_valid": 0.0,
                "grounded_expected_match": 0.0,
                "evidence_strength_match": 1.0,
                "downgraded": 1.0,
            },
            "g2",
        ),
    ]
    metrics = aggregate_grounding(cases)
    assert metrics is not None
    assert metrics.citation_validity_rate == 0.5
    assert metrics.grounded_accuracy == 0.5
    assert metrics.evidence_strength_match_rate == 1.0
    assert metrics.downgrade_rate == 0.5


def test_cases_without_metrics_are_ignored_in_means() -> None:
    cases = [
        _case("retrieval", {"recall": 1.0, "hit": 1.0, "mrr": 1.0, "filter_relaxed": 0.0}, "r1"),
        CaseResult(case_id="r2", suite="retrieval", passed=False, error="LLM failed"),
    ]
    metrics = aggregate_retrieval(cases)
    assert metrics is not None
    assert metrics.cases == 2  # coverage counts the failed case
    assert metrics.recall_at_k == 1.0  # but its missing metrics don't skew means
