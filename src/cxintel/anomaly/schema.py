"""The canonical Anomaly artifact (Phase 4) and the Slack alert contract.

Every detected anomaly is represented by :class:`CanonicalAnomaly` — issue,
derived severity, the independent signals that triggered it (ADR-012), the
supporting metrics, and deterministic human-readable summary / recommended
action. Slack alerts and reports consume this object; they never analyze
operational data themselves.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

AnomalySeverity = Literal["low", "medium", "high", "critical"]


class AnomalySignal(StrEnum):
    """One independent detection signal (anomalies list every signal that fired)."""

    VOLUME_SPIKE = "volume_spike"
    NOVEL_ISSUE = "novel_issue"
    SEVERITY_DRIFT = "severity_drift"
    RESOLUTION_DRIFT = "resolution_drift"


class AnomalyMetrics(BaseModel):
    """The numbers behind an anomaly — every detection is explainable."""

    baseline_count: int
    current_count: int
    percent_change: float | None = None  # None when the baseline is empty (novel)
    baseline_high_severity_share: float | None = None
    current_high_severity_share: float | None = None
    baseline_resolved_share: float | None = None
    current_resolved_share: float | None = None


class CanonicalAnomaly(BaseModel):
    """The canonical artifact consumed by Slack alerts, reports, and dashboards."""

    issue: str
    day: int
    observation_date: datetime | None
    baseline_date: datetime | None
    severity: AnomalySeverity
    signals: list[AnomalySignal]
    metrics: AnomalyMetrics
    summary: str
    recommended_action: str


class SlackAlert(BaseModel):
    """Prompt 3's output contract — a concise operational Slack message."""

    text: str = Field(
        description=(
            "A concise operational Slack alert (2-4 short lines): lead with the "
            "severity and issue name, include the key numbers, end with the "
            "recommended action."
        )
    )
