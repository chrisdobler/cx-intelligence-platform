"""Deterministic artifact comparison (ADR-015).

Every expectation collapses to a flat list of explainable ``CheckResult``
rows: what was checked, what was expected, what the model produced, and
whether it passed. A case passes when all of its checks pass. Free-form prose
is never compared — the checks cover enums, booleans, numeric thresholds,
set membership, and keyword presence only.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from ..resolution_assistant.schema import ContextBundle, ResolutionResponse
from ..understanding.schema import Issue, StructuredConversation
from .golden import (
    ExpectedIssue,
    ExpectedResolution,
    ExpectedUnderstanding,
    SuiteName,
)

CheckKind = Literal["exact", "in_set", "min", "count", "keyword_any", "presence", "execution"]


class CheckResult(BaseModel):
    """One deterministic field-level comparison."""

    check: str = Field(description="Dotted path of what was checked, e.g. 'issue[x].severity'.")
    kind: CheckKind
    expected: str
    actual: str
    passed: bool


class TokenUsageSummary(BaseModel):
    """Token totals of one case's LLM call(s), when the provider reported them."""

    prompt_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class CaseResult(BaseModel):
    """The outcome of one golden case."""

    case_id: str
    suite: SuiteName
    description: str = ""
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None
    tokens: TokenUsageSummary | None = None
    metrics: dict[str, float] = Field(default_factory=dict)


def _check(
    check: str, kind: CheckKind, expected: object, actual: object, passed: bool
) -> CheckResult:
    return CheckResult(
        check=check, kind=kind, expected=str(expected), actual=str(actual), passed=passed
    )


def _exact(check: str, expected: object, actual: object) -> CheckResult:
    return _check(check, "exact", expected, actual, actual == expected)


def _in_set(check: str, allowed: list[str], actual: str) -> CheckResult:
    return _check(check, "in_set", f"one of {allowed}", actual, actual in allowed)


def _min(check: str, minimum: float, actual: float) -> CheckResult:
    return _check(check, "min", f">= {minimum}", f"{actual:.2f}", actual >= minimum)


def _keyword_any(check: str, keywords: list[str], texts: list[str]) -> CheckResult:
    haystack = " ".join(texts).lower()
    passed = any(keyword.lower() in haystack for keyword in keywords)
    return _check(check, "keyword_any", f"any of {keywords}", texts, passed)


def execution_failure(reason: str) -> CheckResult:
    """The single failing check recorded when a case could not execute at all."""
    return _check("execution", "execution", "case executes", reason, False)


# --- understanding -----------------------------------------------------------


def _compare_issue(expected: ExpectedIssue, actual: Issue) -> list[CheckResult]:
    prefix = f"issue[{expected.canonical_name}]"
    checks: list[CheckResult] = []
    if expected.severity_in is not None:
        checks.append(_in_set(f"{prefix}.severity", list(expected.severity_in), actual.severity))
    if expected.customer_impact_in is not None:
        checks.append(
            _in_set(
                f"{prefix}.customer_impact",
                list(expected.customer_impact_in),
                actual.customer_impact,
            )
        )
    if expected.product is not None:
        checks.append(_exact(f"{prefix}.product", expected.product, actual.product))
    if expected.resolution_status is not None:
        checks.append(
            _exact(
                f"{prefix}.resolution_status", expected.resolution_status, actual.resolution_status
            )
        )
    if expected.symptoms_any is not None:
        checks.append(_keyword_any(f"{prefix}.symptoms", expected.symptoms_any, actual.symptoms))
    if expected.catalog_matched is not None:
        checks.append(
            _exact(f"{prefix}.catalog.matched", expected.catalog_matched, actual.catalog.matched)
        )
    if expected.min_confidence is not None:
        checks.append(_min(f"{prefix}.confidence", expected.min_confidence, actual.confidence))
    return checks


def compare_understanding(
    actual: StructuredConversation, expected: ExpectedUnderstanding
) -> list[CheckResult]:
    """Compare a StructuredConversation against its golden expectations."""
    checks: list[CheckResult] = []
    actual_by_name = {issue.canonical_name: issue for issue in actual.issues}

    for expected_issue in expected.issues:
        accepted = [expected_issue.canonical_name, *(expected_issue.canonical_name_aliases or [])]
        actual_issue = next(
            (actual_by_name[name] for name in accepted if name in actual_by_name), None
        )
        found = actual_issue is not None
        checks.append(
            _check(
                f"issue[{expected_issue.canonical_name}].presence",
                "presence",
                f"one of {accepted}" if len(accepted) > 1 else "issue present",
                f"issues: {sorted(actual_by_name)}",
                found,
            )
        )
        if actual_issue is not None:
            checks.extend(_compare_issue(expected_issue, actual_issue))

    if expected.forbid_extra_issues:
        expected_names = {
            name
            for issue in expected.issues
            for name in (issue.canonical_name, *(issue.canonical_name_aliases or []))
        }
        extras = sorted(set(actual_by_name) - expected_names)
        checks.append(
            _check(
                "issues.no_extras", "count", "no unexpected issues", extras or "none", not extras
            )
        )

    if expected.resolution_resolved is not None:
        checks.append(
            _exact("resolution.resolved", expected.resolution_resolved, actual.resolution.resolved)
        )
    if expected.resolution_type is not None:
        checks.append(
            _exact(
                "resolution.resolution_type",
                expected.resolution_type,
                actual.resolution.resolution_type,
            )
        )
    if expected.requires_replacement is not None:
        checks.append(
            _exact(
                "resolution.requires_replacement",
                expected.requires_replacement,
                actual.resolution.requires_replacement,
            )
        )
    if expected.multiple_issues is not None:
        checks.append(
            _exact(
                "conversation.multiple_issues",
                expected.multiple_issues,
                actual.conversation.multiple_issues,
            )
        )
    if expected.requires_followup is not None:
        checks.append(
            _exact(
                "conversation.requires_followup",
                expected.requires_followup,
                actual.conversation.requires_followup,
            )
        )
    if expected.language is not None:
        checks.append(
            _exact("conversation.language", expected.language, actual.conversation.language)
        )
    if expected.min_analysis_confidence is not None:
        checks.append(
            _min(
                "conversation.analysis_confidence",
                expected.min_analysis_confidence,
                actual.conversation.analysis_confidence,
            )
        )
    return checks


# --- retrieval ---------------------------------------------------------------


def score_retrieval(
    retrieved_external_ids: list[str],
    expected_external_ids: list[str],
    *,
    min_recall: float,
    min_precision: float | None = None,
    expect_filter_relaxed: bool | None,
    filter_relaxed: bool,
    kb_external_ids: set[str] | None = None,
) -> tuple[list[CheckResult], dict[str, float]]:
    """Score one retrieval case; returns (checks, per-case metrics).

    ``kb_external_ids`` (when given) lets missing golden sources fail with an
    explicit coverage check instead of a silent recall miss.
    """
    expected = list(dict.fromkeys(expected_external_ids))  # dedupe, keep order
    checks: list[CheckResult] = []

    if kb_external_ids is not None:
        missing = [ext_id for ext_id in expected if ext_id not in kb_external_ids]
        checks.append(
            _check(
                "retrieval.kb_coverage",
                "presence",
                "all expected sources in knowledge base",
                f"missing: {missing}" if missing else "all present",
                not missing,
            )
        )

    hits = [ext_id for ext_id in expected if ext_id in retrieved_external_ids]
    recall = len(hits) / len(expected) if expected else 0.0
    relevant_retrieved = [ext_id for ext_id in retrieved_external_ids if ext_id in expected]
    precision = (
        len(relevant_retrieved) / len(retrieved_external_ids) if retrieved_external_ids else 0.0
    )
    hit = 1.0 if hits else 0.0
    reciprocal_rank = 0.0
    for rank, ext_id in enumerate(retrieved_external_ids, start=1):
        if ext_id in expected:
            reciprocal_rank = 1.0 / rank
            break

    checks.append(
        _check(
            "retrieval.recall",
            "min",
            f">= {min_recall} (expected sources: {expected})",
            f"{recall:.2f} (retrieved: {retrieved_external_ids})",
            recall >= min_recall,
        )
    )
    if min_precision is not None:
        checks.append(
            _check(
                "retrieval.precision",
                "min",
                f">= {min_precision}",
                f"{precision:.2f} (retrieved: {retrieved_external_ids})",
                precision >= min_precision,
            )
        )
    if expect_filter_relaxed is not None:
        checks.append(_exact("retrieval.filter_relaxed", expect_filter_relaxed, filter_relaxed))

    metrics = {
        "recall": recall,
        "precision": precision,
        "hit": hit,
        "mrr": reciprocal_rank,
        "filter_relaxed": 1.0 if filter_relaxed else 0.0,
    }
    return checks, metrics


# --- resolution --------------------------------------------------------------


def compare_resolution(
    raw: ResolutionResponse,
    validated: ResolutionResponse,
    bundle: ContextBundle,
    expected: ExpectedResolution,
    external_id_by_conversation_id: dict[uuid.UUID, str],
) -> tuple[list[CheckResult], dict[str, float]]:
    """Compare a resolution against expectations; returns (checks, grounding metrics).

    Expectations are asserted on the *validated* response (what the platform
    serves); grounding metrics additionally measure the *raw* response so the
    report can show how often the model violated grounding before the
    deterministic validator repaired it.
    """
    checks: list[CheckResult] = []
    valid_doc_ids = {doc.doc_id for doc in bundle.documents}
    conversation_by_doc_id = {doc.doc_id: doc.conversation_id for doc in bundle.documents}

    if expected.grounded is not None:
        checks.append(_exact("response.grounded", expected.grounded, validated.grounded))
    if expected.evidence_strength_in is not None:
        checks.append(
            _in_set(
                "response.evidence_strength",
                list(expected.evidence_strength_in),
                validated.evidence_strength,
            )
        )
    if expected.min_citations is not None:
        checks.append(
            _check(
                "response.citations.min",
                "count",
                f">= {expected.min_citations}",
                validated.citations,
                len(validated.citations) >= expected.min_citations,
            )
        )
    if expected.max_citations is not None:
        checks.append(
            _check(
                "response.citations.max",
                "count",
                f"<= {expected.max_citations}",
                validated.citations,
                len(validated.citations) <= expected.max_citations,
            )
        )
    if expected.citations_from_conversations is not None:
        allowed = set(expected.citations_from_conversations)
        cited_sources = [
            external_id_by_conversation_id.get(
                conversation_by_doc_id.get(doc_id, uuid.UUID(int=0)), f"unknown({doc_id})"
            )
            for doc_id in validated.citations
        ]
        outside = [source for source in cited_sources if source not in allowed]
        checks.append(
            _check(
                "response.citations.provenance",
                "in_set",
                f"all citations from {sorted(allowed)}",
                cited_sources,
                not outside,
            )
        )
    if expected.min_recommended_actions is not None:
        checks.append(
            _check(
                "response.recommended_actions.min",
                "count",
                f">= {expected.min_recommended_actions}",
                f"{len(validated.recommended_actions)} actions",
                len(validated.recommended_actions) >= expected.min_recommended_actions,
            )
        )
    if expected.actions_any_keywords is not None:
        checks.append(
            _keyword_any(
                "response.recommended_actions",
                expected.actions_any_keywords,
                validated.recommended_actions,
            )
        )

    raw_citations_valid = all(citation in valid_doc_ids for citation in raw.citations)
    downgraded = raw.grounded and not validated.grounded
    metrics = {
        "raw_citations_valid": 1.0 if raw_citations_valid else 0.0,
        "grounded_expected_match": (
            1.0 if expected.grounded is None or validated.grounded == expected.grounded else 0.0
        ),
        "evidence_strength_match": (
            1.0
            if expected.evidence_strength_in is None
            or validated.evidence_strength in expected.evidence_strength_in
            else 0.0
        ),
        "downgraded": 1.0 if downgraded else 0.0,
    }
    return checks, metrics
