"""Deterministic anomaly detection rules (ADR-012) — pure functions, no I/O.

The detection engine is explicit business rules over per-day issue statistics:
no LLM, no opaque score. Each of the four signals is evaluated independently;
signals for the same (day, issue) merge into one canonical anomaly that lists
everything that fired and the numbers behind it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..repositories import IssueDayStats
from .schema import AnomalyMetrics, AnomalySeverity, AnomalySignal, CanonicalAnomaly


@dataclass(frozen=True)
class DetectionThresholds:
    """Explicit, configurable detection rules (see Settings)."""

    spike_threshold_pct: float = 50.0  # min % increase vs baseline for a volume spike
    drift_threshold: float = 0.15  # min absolute share change for severity/resolution drift
    min_count: int = 5  # min current-day occurrences for spike/drift signals


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _changed_by(
    baseline: float | None, current: float | None, threshold: float, *, drop_only: bool = False
) -> bool:
    if baseline is None or current is None:
        return False
    delta = current - baseline
    if drop_only:
        return -delta >= threshold
    return abs(delta) >= threshold


def _derive_severity(
    signals: list[AnomalySignal], metrics: AnomalyMetrics, high_share: float | None
) -> AnomalySeverity:
    """Explicit severity rules — documented, not scored."""
    spike = metrics.percent_change or 0.0
    if AnomalySignal.NOVEL_ISSUE in signals and (high_share or 0.0) >= 0.5:
        return "critical"  # brand-new issue category, mostly high-severity reports
    if spike >= 200.0:
        return "critical"
    if len(signals) >= 2 or spike >= 100.0:
        return "high"
    return "medium"


def _summary(issue: str, day: int, signals: list[AnomalySignal], m: AnomalyMetrics) -> str:
    parts: list[str] = []
    if AnomalySignal.NOVEL_ISSUE in signals:
        parts.append(
            f"'{issue}' is a new issue category not in the Day 1 baseline "
            f"({m.current_count} report(s) on day {day})."
        )
    if AnomalySignal.VOLUME_SPIKE in signals and m.percent_change is not None:
        parts.append(
            f"'{issue}' rose {m.percent_change:.0f}% vs the Day 1 baseline "
            f"({m.baseline_count} → {m.current_count})."
        )
    if (
        AnomalySignal.SEVERITY_DRIFT in signals
        and m.baseline_high_severity_share is not None
        and m.current_high_severity_share is not None
    ):
        parts.append(
            f"High/critical share moved from {m.baseline_high_severity_share:.0%} "
            f"to {m.current_high_severity_share:.0%}."
        )
    if (
        AnomalySignal.RESOLUTION_DRIFT in signals
        and m.baseline_resolved_share is not None
        and m.current_resolved_share is not None
    ):
        parts.append(
            f"Resolved share dropped from {m.baseline_resolved_share:.0%} "
            f"to {m.current_resolved_share:.0%}."
        )
    return " ".join(parts)


def _recommended_action(signals: list[AnomalySignal], severity: AnomalySeverity) -> str:
    if AnomalySignal.NOVEL_ISSUE in signals:
        action = "Triage this new issue category and decide whether it belongs in the catalog."
    elif AnomalySignal.RESOLUTION_DRIFT in signals:
        action = "Review recent resolutions for this issue — outcomes are degrading."
    elif AnomalySignal.SEVERITY_DRIFT in signals:
        action = "Investigate why reports of this issue are becoming more severe."
    else:
        action = "Investigate the volume increase and check for a common root cause."
    if severity == "critical":
        action = "Escalate to the on-call owner now. " + action
    return action


def detect(
    baseline: list[IssueDayStats],
    current: list[IssueDayStats],
    *,
    day: int,
    catalog_names: set[str],
    thresholds: DetectionThresholds,
) -> list[CanonicalAnomaly]:
    """Evaluate all four signals for one day against the Day 1 baseline."""
    baseline_by_name = {s.canonical_name: s for s in baseline}
    anomalies: list[CanonicalAnomaly] = []

    for stats in sorted(current, key=lambda s: s.canonical_name):
        base = baseline_by_name.get(stats.canonical_name)
        signals: list[AnomalySignal] = []

        baseline_count = base.count if base else 0
        percent_change = (
            (stats.count - baseline_count) / baseline_count * 100 if baseline_count else None
        )
        baseline_high = _share(base.high_severity_count, base.count) if base else None
        current_high = _share(stats.high_severity_count, stats.count)
        baseline_resolved = _share(base.resolved_count, base.count) if base else None
        current_resolved = _share(stats.resolved_count, stats.count)

        if (
            percent_change is not None
            and percent_change >= thresholds.spike_threshold_pct
            and stats.count >= thresholds.min_count
        ):
            signals.append(AnomalySignal.VOLUME_SPIKE)

        if stats.canonical_name not in catalog_names:
            # Novel categories are anomalous regardless of frequency.
            signals.append(AnomalySignal.NOVEL_ISSUE)

        if stats.count >= thresholds.min_count and _changed_by(
            baseline_high, current_high, thresholds.drift_threshold
        ):
            signals.append(AnomalySignal.SEVERITY_DRIFT)

        if stats.count >= thresholds.min_count and _changed_by(
            baseline_resolved, current_resolved, thresholds.drift_threshold, drop_only=True
        ):
            signals.append(AnomalySignal.RESOLUTION_DRIFT)

        if not signals:
            continue

        metrics = AnomalyMetrics(
            baseline_count=baseline_count,
            current_count=stats.count,
            percent_change=percent_change,
            baseline_high_severity_share=baseline_high,
            current_high_severity_share=current_high,
            baseline_resolved_share=baseline_resolved,
            current_resolved_share=current_resolved,
        )
        severity = _derive_severity(signals, metrics, current_high)
        anomalies.append(
            CanonicalAnomaly(
                issue=stats.canonical_name,
                day=day,
                severity=severity,
                signals=signals,
                metrics=metrics,
                summary=_summary(stats.canonical_name, day, signals, metrics),
                recommended_action=_recommended_action(signals, severity),
            )
        )
    return anomalies
