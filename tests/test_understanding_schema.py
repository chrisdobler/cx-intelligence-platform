"""Tests for the canonical StructuredConversation contract (frozen V1)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cxintel.understanding.schema import Issue, StructuredConversation

# The exact example from docs/PHASE3-UNDERSTANDING.md (Version 1, frozen).
FROZEN_V1_EXAMPLE: dict[str, Any] = {
    "summary": {
        "short": "Customer reported a leaking Pod 5 base; replacement arranged.",
        "detailed": "The customer contacted support about water pooling under their "
        "Pod 5. The agent confirmed a known base-seal defect and arranged a "
        "replacement unit.",
    },
    "issues": [
        {
            "canonical_name": "base water leak",
            "customer_description": "there's water pooling under the pod every morning",
            "severity": "high",
            "confidence": 0.96,
            "customer_impact": "high",
            "product": "Pod 5",
            "symptoms": ["water pooling under unit", "damp flooring"],
            "catalog": {"matched": True, "confidence": 0.98},
            "resolution_status": "resolved",
            "resolution_summary": "Replacement unit shipped.",
        }
    ],
    "resolution": {
        "resolved": True,
        "resolution_type": "replacement",
        "summary": "Replacement unit arranged after confirming a base-seal defect.",
        "actions": ["confirmed defect", "arranged replacement"],
        "requires_replacement": False,
    },
    "conversation": {
        "language": "English",
        "multiple_issues": True,
        "requires_followup": False,
        "customer_emotion": "frustrated",
        "analysis_confidence": 0.94,
    },
}


def test_frozen_v1_example_validates() -> None:
    sc = StructuredConversation.model_validate(FROZEN_V1_EXAMPLE)
    assert sc.summary.short.startswith("Customer reported")
    assert len(sc.issues) == 1
    issue = sc.issues[0]
    assert issue.canonical_name == "base water leak"
    assert issue.catalog.matched is True
    assert sc.resolution.resolution_type == "replacement"
    assert sc.conversation.analysis_confidence == 0.94


def test_model_dump_round_trips_unchanged() -> None:
    """The persisted JSONB must be the validated object, byte-for-byte re-loadable."""
    sc = StructuredConversation.model_validate(FROZEN_V1_EXAMPLE)
    dumped = sc.model_dump()
    assert StructuredConversation.model_validate(dumped).model_dump() == dumped
    assert dumped == FROZEN_V1_EXAMPLE  # no coercion drift on the frozen example


def test_zero_issues_is_legitimate() -> None:
    data = {**FROZEN_V1_EXAMPLE, "issues": []}
    assert StructuredConversation.model_validate(data).issues == []


def test_confidence_out_of_range_rejected() -> None:
    bad = {**FROZEN_V1_EXAMPLE}
    bad["issues"] = [{**FROZEN_V1_EXAMPLE["issues"][0], "confidence": 1.7}]
    with pytest.raises(ValidationError):
        StructuredConversation.model_validate(bad)


def test_unknown_severity_rejected() -> None:
    bad = {**FROZEN_V1_EXAMPLE}
    bad["issues"] = [{**FROZEN_V1_EXAMPLE["issues"][0], "severity": "catastrophic"}]
    with pytest.raises(ValidationError):
        StructuredConversation.model_validate(bad)


def test_resolution_summary_may_be_null() -> None:
    data = {**FROZEN_V1_EXAMPLE}
    data["issues"] = [{**FROZEN_V1_EXAMPLE["issues"][0], "resolution_summary": None}]
    issue = StructuredConversation.model_validate(data).issues[0]
    assert issue.resolution_summary is None


def test_missing_required_field_rejected() -> None:
    bad = {**FROZEN_V1_EXAMPLE}
    bad["issues"] = [
        {k: v for k, v in FROZEN_V1_EXAMPLE["issues"][0].items() if k != "canonical_name"}
    ]
    with pytest.raises(ValidationError):
        StructuredConversation.model_validate(bad)


def test_every_field_has_a_description() -> None:
    """Field descriptions feed the provider-generated schema — they are the contract."""
    for model in (StructuredConversation, Issue):
        for name, field in model.model_fields.items():
            assert field.description, f"{model.__name__}.{name} is missing a description"
