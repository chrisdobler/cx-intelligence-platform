"""Anomaly detection — Phase 4 pipeline stage business logic.

Consumes only relational projections (``conversation_issues``, the issue
catalog, and conversation day metadata) — raw conversations are never
reparsed, and the LLM plays no part in detection (ADR-012). The deterministic
detector compares each post-baseline day against Day 1; the resulting
canonical anomalies are persisted (regenerated wholesale each run — derived
data), converted to Slack alerts via Prompt #3 (with a deterministic fallback
so alert prose can never fail the run), optionally delivered to a webhook,
and written out as the anomaly report.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy.orm import Session, sessionmaker

from ..llm import LLMExtractionError, LLMProvider
from ..models import Anomaly
from ..pipeline.progress import ProgressCallback, ProgressReporter
from ..repositories import (
    AnomalyRepository,
    ConversationIssueRepository,
    ConversationRepository,
    IssueCatalogRepository,
)
from .detector import DetectionThresholds, detect
from .prompt import build_slack_prompt, fallback_slack_message
from .schema import CanonicalAnomaly, SlackAlert

logger = logging.getLogger(__name__)

_WEBHOOK_TIMEOUT_SECONDS = 10.0


def _noop_progress(_message: object) -> None:
    return None


class AnomalyResult:
    """Outcome of one anomaly-detection run."""

    def __init__(self) -> None:
        self.anomalies = 0
        self.by_signal: dict[str, int] = {}
        self.alerts_delivered = 0
        self.alert_fallbacks = 0
        self.baseline_only = False
        self.report_path: Path | None = None
        self.webhook_configured = False

    def summary(self) -> str:
        if self.baseline_only:
            return "Only the baseline day is analyzed — nothing to compare yet."
        signals = ", ".join(f"{count} {name}" for name, count in sorted(self.by_signal.items()))
        parts = [f"Detected {self.anomalies} anomalies" + (f" ({signals})." if signals else ".")]
        if self.anomalies:
            if self.webhook_configured:
                parts.append(f"Slack: {self.alerts_delivered} delivered.")
            else:
                parts.append("Slack: delivery skipped (SLACK_WEBHOOK_URL unset).")
            if self.alert_fallbacks:
                parts.append(f"{self.alert_fallbacks} alert(s) used the fallback template.")
        if self.report_path is not None:
            parts.append(f"Report: {self.report_path}")
        return " ".join(parts)


class AnomalyService:
    """Runs deterministic anomaly detection over the issue projections."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: LLMProvider,
        *,
        pipeline_run_id: uuid.UUID | None = None,
        report_path: Path | None = None,
        min_count: int | None = None,
    ) -> None:
        from ..config import get_settings

        settings = get_settings()
        self._session_factory = session_factory
        self._provider = provider
        self._pipeline_run_id = pipeline_run_id
        self._report_path = report_path or Path(settings.anomaly_report_path)
        self._thresholds = DetectionThresholds(
            spike_threshold_pct=settings.anomaly_spike_threshold_pct,
            drift_threshold=settings.anomaly_drift_threshold,
            min_count=min_count if min_count is not None else settings.anomaly_min_count,
        )
        self._webhook_url = settings.slack_webhook_url

    def run(self, progress: ProgressCallback | ProgressReporter = _noop_progress) -> AnomalyResult:
        reporter = (
            progress
            if isinstance(progress, ProgressReporter)
            else ProgressReporter(
                stage_key="anomaly",
                stage_label="Anomaly Detection",
                progress=progress,
                message="Preparing anomaly detection…",
            )
        )
        result = AnomalyResult()
        result.webhook_configured = bool(self._webhook_url)

        with self._session_factory() as session:
            days = ConversationRepository(session).days()
            issues = ConversationIssueRepository(session)
            catalog_names = {e.canonical_name for e in IssueCatalogRepository(session).all()}
            baseline_day = days[0] if days else None
            baseline = issues.day_issue_stats(baseline_day) if baseline_day is not None else []
            later_days = [d for d in days if baseline_day is not None and d > baseline_day]
            per_day_stats = {day: issues.day_issue_stats(day) for day in later_days}

        comparable_days = [d for d in later_days if per_day_stats[d]]
        if not comparable_days:
            result.baseline_only = True
            reporter.report(message=result.summary())
            return result

        reporter.report(
            total_work=len(comparable_days),
            message=f"Comparing {len(comparable_days)} day(s) against the Day 1 baseline…",
        )

        anomalies: list[CanonicalAnomaly] = []
        for day in comparable_days:
            detected = detect(
                baseline,
                per_day_stats[day],
                day=day,
                catalog_names=catalog_names,
                thresholds=self._thresholds,
            )
            anomalies.extend(detected)
            reporter.advance(
                current_item=f"day {day}",
                message=f"Day {day}: {len(detected)} anomalies detected.",
            )

        # Persist the canonical artifact FIRST (with deterministic alert text)
        # so alert prose — an LLM nicety with a fallback — can never block it.
        reporter.report(
            total_work=len(comparable_days) + len(anomalies),
            message=f"{len(anomalies)} anomalies detected — persisting…",
        )
        rows = self._persist(anomalies, [fallback_slack_message(a) for a in anomalies])

        for anomaly, row in zip(anomalies, rows, strict=True):
            text = self._slack_alert(anomaly, result, reporter)
            if text != row.slack_message:
                self._update_alert(row.id, text)
                row.slack_message = text
            self._deliver_one(text, result)
            reporter.advance(
                current_item=anomaly.issue,
                message=f"Alert ready for '{anomaly.issue}'.",
            )
        result.report_path = self._write_report(rows)

        result.anomalies = len(anomalies)
        for anomaly in anomalies:
            for signal in anomaly.signals:
                result.by_signal[signal.value] = result.by_signal.get(signal.value, 0) + 1
        reporter.report(message=result.summary())
        return result

    # -- alerts --------------------------------------------------------------

    def _slack_alert(
        self, anomaly: CanonicalAnomaly, result: AnomalyResult, reporter: ProgressReporter
    ) -> str:
        try:
            return self._provider.extract(build_slack_prompt(anomaly), SlackAlert).text
        except LLMExtractionError as exc:
            logger.warning("slack alert generation failed for %s: %s", anomaly.issue, exc)
            result.alert_fallbacks += 1
            reporter.report(message=f"Alert for '{anomaly.issue}' used the fallback template.")
            return fallback_slack_message(anomaly)

    def _deliver_one(self, text: str, result: AnomalyResult) -> None:
        if not self._webhook_url:
            return
        try:
            response = httpx.post(
                self._webhook_url, json={"text": text}, timeout=_WEBHOOK_TIMEOUT_SECONDS
            )
            if response.status_code < 300:
                result.alerts_delivered += 1
            else:
                logger.warning("slack webhook returned %s", response.status_code)
        except Exception as exc:  # delivery is best-effort, never fatal
            logger.warning("slack webhook delivery failed: %s", exc)

    # -- persistence + report --------------------------------------------------

    def _persist(self, anomalies: list[CanonicalAnomaly], alerts: list[str]) -> list[Anomaly]:
        now = datetime.now(tz=UTC)
        rows = [
            Anomaly(
                id=uuid.uuid4(),
                day=anomaly.day,
                issue=anomaly.issue,
                severity=anomaly.severity,
                delta=anomaly.metrics.percent_change or 0.0,
                description=anomaly.summary,
                slack_message=alert,
                signals=[s.value for s in anomaly.signals],
                metrics=anomaly.metrics.model_dump(),
                recommended_action=anomaly.recommended_action,
                created_at=now,
            )
            for anomaly, alert in zip(anomalies, alerts, strict=True)
        ]
        with self._session_factory() as session:
            AnomalyRepository(session).replace_all(rows)
            session.commit()
        return rows

    def _update_alert(self, anomaly_id: uuid.UUID, text: str) -> None:
        with self._session_factory() as session:
            row = session.get(Anomaly, anomaly_id)
            if row is not None:
                row.slack_message = text
                session.commit()

    def _write_report(self, rows: list[Anomaly]) -> Path:
        from .reporting import render_report

        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        self._report_path.write_text(render_report(rows), encoding="utf-8")
        return self._report_path
