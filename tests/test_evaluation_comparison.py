"""Deterministic comparators — every rule kind, and prose provably ignored."""

from __future__ import annotations

import uuid
from typing import Any

from cxintel.evaluation.comparison import (
    CheckResult,
    compare_resolution,
    compare_understanding,
    score_retrieval,
)
from cxintel.evaluation.golden import ExpectedIssue, ExpectedResolution, ExpectedUnderstanding
from cxintel.knowledge_base.schema import KnowledgeDocument
from cxintel.resolution_assistant.context import validate_citations
from cxintel.resolution_assistant.schema import (
    ContextBundle,
    ContextDocument,
    ResolutionResponse,
    RetrievalMetadata,
)

from .test_knowledge_generation import make_issue, make_structured


def _failed(checks: list[CheckResult]) -> list[str]:
    return [c.check for c in checks if not c.passed]


# --- understanding ----------------------------------------------------------


def test_all_constraints_pass_on_matching_artifact() -> None:
    actual = make_structured(
        [make_issue("base water leak", symptoms=["water pooling under the base"])],
        resolved=True,
        resolution_type="replacement",
        requires_replacement=True,
    )
    expected = ExpectedUnderstanding(
        issues=[
            ExpectedIssue(
                canonical_name="base water leak",
                severity_in=["medium", "high"],
                customer_impact_in=["high"],
                product="Pod 5",
                resolution_status="resolved",
                symptoms_any=["WATER"],
                catalog_matched=True,
                min_confidence=0.5,
            )
        ],
        forbid_extra_issues=True,
        resolution_resolved=True,
        resolution_type="replacement",
        requires_replacement=True,
        multiple_issues=False,
        requires_followup=False,
        language="English",
        min_analysis_confidence=0.5,
    )
    checks = compare_understanding(actual, expected)
    assert checks and all(c.passed for c in checks)


def test_prose_differences_are_never_compared() -> None:
    expected = ExpectedUnderstanding(
        issues=[ExpectedIssue(canonical_name="base water leak")],
        resolution_resolved=True,
    )
    a = make_structured([make_issue("base water leak")])
    b = a.model_copy(deep=True)
    b.summary.short = "completely different prose"
    b.summary.detailed = "other prose"
    b.issues[0].customer_description = "reworded"
    b.resolution.summary = "different wording"
    checks_a = compare_understanding(a, expected)
    checks_b = compare_understanding(b, expected)
    assert [(c.check, c.passed) for c in checks_a] == [(c.check, c.passed) for c in checks_b]
    assert all(c.passed for c in checks_b)


def test_missing_issue_fails_presence_and_skips_field_checks() -> None:
    actual = make_structured([make_issue("something else")])
    expected = ExpectedUnderstanding(
        issues=[ExpectedIssue(canonical_name="base water leak", severity_in=["high"])]
    )
    checks = compare_understanding(actual, expected)
    assert _failed(checks) == ["issue[base water leak].presence"]


def test_alias_names_count_as_the_same_issue() -> None:
    actual = make_structured([make_issue("pod water leak")])
    expected = ExpectedUnderstanding(
        issues=[
            ExpectedIssue(
                canonical_name="base water leak",
                canonical_name_aliases=["pod water leak"],
                severity_in=["medium"],
            )
        ],
        forbid_extra_issues=True,
    )
    checks = compare_understanding(actual, expected)
    assert all(c.passed for c in checks)


def test_each_rule_kind_fails_on_mismatch() -> None:
    actual = make_structured([make_issue("base water leak", symptoms=["no match here"])])
    expected = ExpectedUnderstanding(
        issues=[
            ExpectedIssue(
                canonical_name="base water leak",
                severity_in=["critical"],  # actual: medium
                product="Pod 9",  # actual: Pod 5
                symptoms_any=["banana"],  # not present
                min_confidence=0.99,  # actual: 0.9
            )
        ],
        multiple_issues=True,  # actual: False
    )
    failed = _failed(compare_understanding(actual, expected))
    assert set(failed) == {
        "issue[base water leak].severity",
        "issue[base water leak].product",
        "issue[base water leak].symptoms",
        "issue[base water leak].confidence",
        "conversation.multiple_issues",
    }


def test_forbid_extra_issues_flags_unexpected_ones() -> None:
    actual = make_structured([make_issue("base water leak"), make_issue("surprise issue")])
    expected = ExpectedUnderstanding(
        issues=[ExpectedIssue(canonical_name="base water leak")],
        forbid_extra_issues=True,
    )
    assert _failed(compare_understanding(actual, expected)) == ["issues.no_extras"]


# --- retrieval --------------------------------------------------------------


def test_retrieval_scoring_metrics() -> None:
    checks, metrics = score_retrieval(
        ["c", "a", "x"],
        ["a", "b"],
        min_recall=0.5,
        expect_filter_relaxed=False,
        filter_relaxed=False,
        kb_external_ids={"a", "b", "c", "x"},
    )
    assert metrics == {
        "recall": 0.5,
        "precision": 1 / 3,
        "hit": 1.0,
        "mrr": 0.5,
        "filter_relaxed": 0.0,
    }
    assert all(c.passed for c in checks)


def test_retrieval_precision_check() -> None:
    checks, metrics = score_retrieval(
        ["a", "x", "b"],
        ["a", "b", "c", "d"],
        min_recall=0.0,
        min_precision=0.8,
        expect_filter_relaxed=None,
        filter_relaxed=False,
    )
    assert metrics["precision"] == 2 / 3
    assert _failed(checks) == ["retrieval.precision"]


def test_retrieval_recall_below_threshold_fails() -> None:
    checks, metrics = score_retrieval(
        ["x", "y"],
        ["a", "b"],
        min_recall=0.5,
        expect_filter_relaxed=None,
        filter_relaxed=False,
    )
    assert metrics["recall"] == 0.0 and metrics["mrr"] == 0.0 and metrics["hit"] == 0.0
    assert _failed(checks) == ["retrieval.recall"]


def test_retrieval_missing_kb_coverage_is_an_explicit_failure() -> None:
    checks, _ = score_retrieval(
        [],
        ["a"],
        min_recall=0.0,
        expect_filter_relaxed=None,
        filter_relaxed=False,
        kb_external_ids={"other"},
    )
    assert "retrieval.kb_coverage" in _failed(checks)


def test_retrieval_filter_relaxed_expectation() -> None:
    checks, metrics = score_retrieval(
        ["a"],
        ["a"],
        min_recall=1.0,
        expect_filter_relaxed=True,
        filter_relaxed=False,
    )
    assert _failed(checks) == ["retrieval.filter_relaxed"]
    assert metrics["filter_relaxed"] == 0.0


# --- resolution -------------------------------------------------------------


def _bundle(doc_count: int = 2) -> tuple[ContextBundle, dict[uuid.UUID, str]]:
    ids = [uuid.uuid4() for _ in range(doc_count)]
    documents = [
        ContextDocument(
            doc_id=f"KB-{rank}",
            conversation_id=conv_id,
            distance=0.1 * rank,
            document=KnowledgeDocument(
                issue="base water leak",
                product="Pod 5",
                symptoms=["water"],
                prerequisites=[],
                resolution_type="replacement",
                resolution_summary="replaced the base seal",
                actions=["ship replacement"],
                outcome="resolved",
            ),
        )
        for rank, conv_id in enumerate(ids, start=1)
    ]
    bundle = ContextBundle(
        issue=make_issue("base water leak"),
        documents=documents,
        retrieval=RetrievalMetadata(
            query_text="q",
            product_filter="Pod 5",
            filter_relaxed=False,
            limit=5,
            result_count=doc_count,
            distances=[d.distance for d in documents],
        ),
    )
    external = {conv_id: f"conv_{i}" for i, conv_id in enumerate(ids, start=1)}
    return bundle, external


def _response(**overrides: Any) -> ResolutionResponse:
    base: dict[str, Any] = {
        "recommendation": "Replace the base.",
        "reasoning": "KB-1 matches.",
        "recommended_actions": ["Ship a replacement base"],
        "grounded": True,
        "evidence_strength": "strong",
        "citations": ["KB-1"],
    }
    base.update(overrides)
    return ResolutionResponse(**base)


def test_resolution_expectations_pass() -> None:
    bundle, external = _bundle()
    raw = _response()
    validated = validate_citations(raw, bundle)
    expected = ExpectedResolution(
        grounded=True,
        evidence_strength_in=["moderate", "strong"],
        min_citations=1,
        max_citations=2,
        citations_from_conversations=["conv_1"],
        min_recommended_actions=1,
        actions_any_keywords=["replacement"],
    )
    checks, metrics = compare_resolution(raw, validated, bundle, expected, external)
    assert all(c.passed for c in checks)
    assert metrics == {
        "raw_citations_valid": 1.0,
        "grounded_expected_match": 1.0,
        "evidence_strength_match": 1.0,
        "downgraded": 0.0,
    }


def test_resolution_invalid_citation_measured_and_downgrade_detected() -> None:
    bundle, external = _bundle()
    raw = _response(citations=["KB-9"])  # not in the bundle
    validated = validate_citations(raw, bundle)  # downgrades to ungrounded
    expected = ExpectedResolution(grounded=True, min_citations=1)
    checks, metrics = compare_resolution(raw, validated, bundle, expected, external)
    assert metrics["raw_citations_valid"] == 0.0
    assert metrics["downgraded"] == 1.0
    assert set(_failed(checks)) == {"response.grounded", "response.citations.min"}


def test_resolution_citation_provenance_failure() -> None:
    bundle, external = _bundle()
    raw = _response(citations=["KB-2"])
    validated = validate_citations(raw, bundle)
    expected = ExpectedResolution(citations_from_conversations=["conv_1"])
    checks, _ = compare_resolution(raw, validated, bundle, expected, external)
    assert _failed(checks) == ["response.citations.provenance"]


def test_resolution_prose_never_compared() -> None:
    bundle, external = _bundle()
    expected = ExpectedResolution(grounded=True, min_citations=1)
    raw_a = _response()
    raw_b = _response(recommendation="entirely different words", reasoning="other reasoning")
    checks_a, _ = compare_resolution(
        raw_a, validate_citations(raw_a, bundle), bundle, expected, external
    )
    checks_b, _ = compare_resolution(
        raw_b, validate_citations(raw_b, bundle), bundle, expected, external
    )
    assert [(c.check, c.passed) for c in checks_a] == [(c.check, c.passed) for c in checks_b]
