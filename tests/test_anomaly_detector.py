"""Pure unit tests for the deterministic anomaly detection rules (no DB, no LLM)."""

from __future__ import annotations

from cxintel.anomaly.detector import DetectionThresholds, detect
from cxintel.anomaly.schema import AnomalySignal, CanonicalAnomaly
from cxintel.repositories import IssueDayStats


def stats(
    name: str,
    count: int,
    *,
    high_severity: int = 0,
    resolved: int | None = None,
    unmatched: int = 0,
) -> IssueDayStats:
    return IssueDayStats(
        canonical_name=name,
        count=count,
        high_severity_count=high_severity,
        resolved_count=count if resolved is None else resolved,
        unmatched_count=unmatched,
    )


THRESHOLDS = DetectionThresholds(spike_threshold_pct=50.0, drift_threshold=0.15, min_count=5)


def run_detect(
    baseline: list[IssueDayStats],
    current: list[IssueDayStats],
    *,
    day: int = 2,
    catalog: set[str] | None = None,
) -> list[CanonicalAnomaly]:
    if catalog is None:
        catalog = {s.canonical_name for s in baseline}
    return detect(baseline, current, day=day, catalog_names=catalog, thresholds=THRESHOLDS)


# --- volume spike -------------------------------------------------------------


def test_volume_spike_detected_above_threshold() -> None:
    anomalies = run_detect([stats("leak", 30)], [stats("leak", 97)])
    assert len(anomalies) == 1
    anomaly = anomalies[0]
    assert anomaly.issue == "leak"
    assert anomaly.day == 2
    assert AnomalySignal.VOLUME_SPIKE in anomaly.signals
    assert anomaly.metrics.baseline_count == 30
    assert anomaly.metrics.current_count == 97
    assert anomaly.metrics.percent_change is not None
    assert round(anomaly.metrics.percent_change) == 223
    # The explanation carries the numbers.
    assert "97" in anomaly.summary and "30" in anomaly.summary
    assert anomaly.recommended_action


def test_no_spike_below_threshold() -> None:
    assert run_detect([stats("leak", 30)], [stats("leak", 40)]) == []  # +33% < 50%


def test_spike_needs_min_count() -> None:
    # 1 → 4 is a 300% increase but below min_count — noise, not an anomaly.
    assert run_detect([stats("rare", 1)], [stats("rare", 4)]) == []


def test_volume_drop_is_not_a_spike() -> None:
    assert run_detect([stats("leak", 90)], [stats("leak", 20)]) == []


# --- novel issue ----------------------------------------------------------------


def test_novel_issue_detected_regardless_of_frequency() -> None:
    anomalies = run_detect([stats("leak", 30)], [stats("leak", 31), stats("brand new", 1)])
    assert len(anomalies) == 1
    anomaly = anomalies[0]
    assert anomaly.issue == "brand new"
    assert anomaly.signals == [AnomalySignal.NOVEL_ISSUE]
    assert anomaly.metrics.baseline_count == 0
    assert anomaly.metrics.percent_change is None


# --- severity drift -------------------------------------------------------------


def test_severity_drift_detected() -> None:
    baseline = [stats("leak", 30, high_severity=3)]  # 10% high
    current = [stats("leak", 32, high_severity=16)]  # 50% high — +40 points
    anomalies = run_detect(baseline, current)
    assert len(anomalies) == 1
    assert anomalies[0].signals == [AnomalySignal.SEVERITY_DRIFT]
    assert anomalies[0].metrics.baseline_high_severity_share == 0.1
    assert anomalies[0].metrics.current_high_severity_share == 0.5


def test_severity_drift_below_threshold_ignored() -> None:
    baseline = [stats("leak", 30, high_severity=3)]  # 10%
    current = [stats("leak", 30, high_severity=6)]  # 20% — +10 points < 15
    assert run_detect(baseline, current) == []


# --- resolution drift -----------------------------------------------------------


def test_resolution_drift_detected_on_resolved_share_drop() -> None:
    baseline = [stats("leak", 30, resolved=27)]  # 90% resolved
    current = [stats("leak", 30, resolved=15)]  # 50% resolved — -40 points
    anomalies = run_detect(baseline, current)
    assert len(anomalies) == 1
    assert anomalies[0].signals == [AnomalySignal.RESOLUTION_DRIFT]
    assert anomalies[0].metrics.baseline_resolved_share == 0.9
    assert anomalies[0].metrics.current_resolved_share == 0.5


def test_resolution_improvement_is_not_drift() -> None:
    baseline = [stats("leak", 30, resolved=15)]
    current = [stats("leak", 30, resolved=29)]
    assert run_detect(baseline, current) == []


# --- signal merging + severity rules ---------------------------------------------


def test_multiple_signals_merge_into_one_anomaly() -> None:
    baseline = [stats("leak", 30, high_severity=3, resolved=27)]
    current = [stats("leak", 97, high_severity=60, resolved=30)]
    anomalies = run_detect(baseline, current)
    assert len(anomalies) == 1
    signals = set(anomalies[0].signals)
    assert {
        AnomalySignal.VOLUME_SPIKE,
        AnomalySignal.SEVERITY_DRIFT,
        AnomalySignal.RESOLUTION_DRIFT,
    } <= signals


def test_severity_rules() -> None:
    # Spike ≥ 200% → critical.
    critical = run_detect([stats("leak", 30)], [stats("leak", 97)])[0]
    assert critical.severity == "critical"
    # Single moderate spike (50-100%) → medium.
    medium = run_detect([stats("leak", 30)], [stats("leak", 48)])[0]
    assert medium.severity == "medium"
    # Two signals → at least high.
    baseline = [stats("leak", 30, high_severity=3)]
    current = [stats("leak", 50, high_severity=30)]
    multi = run_detect(baseline, current)[0]
    assert multi.severity in ("high", "critical")
    # Novel issue with mostly high-severity occurrences → critical.
    novel = run_detect(
        [stats("leak", 30)], [stats("leak", 30), stats("fire hazard", 6, high_severity=6)]
    )
    novel_anomaly = next(a for a in novel if a.issue == "fire hazard")
    assert novel_anomaly.severity == "critical"


def test_detection_is_deterministic_and_sorted() -> None:
    baseline = [stats("a", 30), stats("b", 30)]
    current = [stats("b", 90), stats("a", 90), stats("z novel", 2)]
    first = run_detect(baseline, current)
    second = run_detect(baseline, current)
    assert [a.issue for a in first] == [a.issue for a in second]
    assert len(first) == 3
