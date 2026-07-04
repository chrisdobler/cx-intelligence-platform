"""Golden evaluation dataset — schema and loader (Phase 7).

One JSON file per case under ``evals/golden/{understanding,retrieval,resolution}/``,
validated against these models on load (``extra="forbid"`` so a typo in a case
file fails loudly instead of silently not checking anything).

Expected artifacts are *constrained subsets* of the canonical schemas: a case
asserts only the fields its author is confident about — every omitted field is
simply not checked. Free-form prose (summaries, reasoning, recommendations) is
never part of an expectation (ADR-015: compare structured outputs, not text).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..understanding.schema import CustomerImpact, Issue, ResolutionStatus, Severity

DEFAULT_GOLDEN_ROOT = Path("evals/golden")

SUITES = ("understanding", "retrieval", "resolution")
SuiteName = Literal["understanding", "retrieval", "resolution"]


class GoldenDatasetError(Exception):
    """Raised when the golden dataset is missing or fails validation."""


class _GoldenModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- understanding cases -----------------------------------------------------


class TransientConversationInput(_GoldenModel):
    """Metadata for the transient conversation fed to Prompt #1."""

    product: str = "unknown"
    category: str = "unknown"
    priority: str = "unknown"
    status: str = "open"


class TransientMessageInput(_GoldenModel):
    """One transcript message of an understanding case."""

    role: str
    body: str


class ExpectedIssue(_GoldenModel):
    """Constrained expectations for one issue, matched by canonical name."""

    canonical_name: str = Field(description="Exact lowercase canonical name the issue must have.")
    canonical_name_aliases: list[str] | None = Field(
        default=None,
        description=(
            "Additional acceptable canonical names — the catalog contains "
            "near-synonym categories, and any of these count as the same issue."
        ),
    )
    severity_in: list[Severity] | None = None
    customer_impact_in: list[CustomerImpact] | None = None
    product: str | None = None
    resolution_status: ResolutionStatus | None = None
    symptoms_any: list[str] | None = Field(
        default=None,
        description="At least one keyword must appear (case-insensitive) in the symptoms.",
    )
    catalog_matched: bool | None = None
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ExpectedUnderstanding(_GoldenModel):
    """Constrained expectations over one StructuredConversation."""

    issues: list[ExpectedIssue] = Field(default_factory=list)
    forbid_extra_issues: bool = False
    resolution_resolved: bool | None = None
    resolution_type: str | None = None
    requires_replacement: bool | None = None
    multiple_issues: bool | None = None
    requires_followup: bool | None = None
    language: str | None = None
    min_analysis_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class UnderstandingCase(_GoldenModel):
    """Transcript in, expected StructuredConversation constraints out."""

    case_id: str
    description: str
    conversation: TransientConversationInput = Field(default_factory=TransientConversationInput)
    messages: list[TransientMessageInput] = Field(min_length=1)
    expected: ExpectedUnderstanding


# --- retrieval cases ---------------------------------------------------------


class RetrievalCase(_GoldenModel):
    """Issue in, expected knowledge-base sources in the top-k out.

    Expected results reference source conversations by ``external_id``
    (``TICKET-xxxx``) — the only identifier stable across database rebuilds.
    """

    case_id: str
    description: str
    issue: Issue
    limit: int = Field(default=5, ge=1)
    expected_conversation_external_ids: list[str] = Field(min_length=1)
    expect_filter_relaxed: bool | None = None
    min_recall: float = Field(default=1.0, ge=0.0, le=1.0)
    min_precision: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum fraction of retrieved documents whose source is in the "
            "expected set — use instead of recall when the relevant pool is "
            "larger than the top-k."
        ),
    )


# --- resolution cases --------------------------------------------------------


class ExpectedResolution(_GoldenModel):
    """Constrained expectations over one ResolutionResponse."""

    grounded: bool | None = None
    evidence_strength_in: list[Literal["none", "weak", "moderate", "strong"]] | None = None
    min_citations: int | None = Field(default=None, ge=0)
    max_citations: int | None = Field(default=None, ge=0)
    citations_from_conversations: list[str] | None = Field(
        default=None,
        description=(
            "Every cited document must originate from one of these source "
            "conversation external ids."
        ),
    )
    min_recommended_actions: int | None = Field(default=None, ge=0)
    actions_any_keywords: list[str] | None = Field(
        default=None,
        description=(
            "At least one keyword must appear (case-insensitive) in the recommended actions."
        ),
    )


class ResolutionCase(_GoldenModel):
    """Issue in, expected ResolutionResponse constraints out."""

    case_id: str
    description: str
    issue: Issue
    limit: int = Field(default=5, ge=1)
    expected: ExpectedResolution


# --- the dataset -------------------------------------------------------------


class DatasetInfo(_GoldenModel):
    """The version stamp in ``evals/golden/dataset.json``."""

    version: str
    description: str = ""


class GoldenDataset(BaseModel):
    """The full, validated golden dataset."""

    version: str
    description: str = ""
    understanding: list[UnderstandingCase] = Field(default_factory=list)
    retrieval: list[RetrievalCase] = Field(default_factory=list)
    resolution: list[ResolutionCase] = Field(default_factory=list)

    @property
    def total_cases(self) -> int:
        return len(self.understanding) + len(self.retrieval) + len(self.resolution)

    def coverage(self) -> dict[str, int]:
        return {
            "understanding": len(self.understanding),
            "retrieval": len(self.retrieval),
            "resolution": len(self.resolution),
        }


def _load_case_file[CaseT: (UnderstandingCase, RetrievalCase, ResolutionCase)](
    path: Path, model: type[CaseT]
) -> CaseT:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise GoldenDatasetError(f"Invalid JSON in {path}: {exc}") from exc
    try:
        return model.model_validate(data)
    except Exception as exc:
        raise GoldenDatasetError(f"Invalid golden case {path}: {exc}") from exc


def _load_suite[CaseT: (UnderstandingCase, RetrievalCase, ResolutionCase)](
    root: Path, suite: str, model: type[CaseT], seen_ids: set[str]
) -> list[CaseT]:
    cases: list[CaseT] = []
    for path in sorted((root / suite).glob("*.json")):
        case = _load_case_file(path, model)
        if case.case_id in seen_ids:
            raise GoldenDatasetError(f"Duplicate case_id '{case.case_id}' (found in {path}).")
        seen_ids.add(case.case_id)
        cases.append(case)
    return cases


def load_golden_dataset(root: Path = DEFAULT_GOLDEN_ROOT) -> GoldenDataset:
    """Load and validate the whole golden dataset (deterministic file order)."""
    info_path = root / "dataset.json"
    if not info_path.exists():
        raise GoldenDatasetError(
            f"Golden dataset not found at {root} — expected {info_path} to exist."
        )
    info = DatasetInfo.model_validate(json.loads(info_path.read_text()))

    seen_ids: set[str] = set()
    return GoldenDataset(
        version=info.version,
        description=info.description,
        understanding=_load_suite(root, "understanding", UnderstandingCase, seen_ids),
        retrieval=_load_suite(root, "retrieval", RetrievalCase, seen_ids),
        resolution=_load_suite(root, "resolution", ResolutionCase, seen_ids),
    )
