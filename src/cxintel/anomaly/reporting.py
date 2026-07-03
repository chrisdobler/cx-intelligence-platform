"""Anomaly report rendering — one renderer for the file, the API, and the CLI.

Reports consume persisted anomalies (the canonical artifact); they never
analyze operational data themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from ..models import Anomaly


def render_report(anomalies: Sequence[Anomaly]) -> str:
    """Render the markdown anomaly report from persisted anomaly rows."""
    lines = [
        "# Anomaly Report",
        "",
        f"Generated: {datetime.now(tz=UTC).isoformat(timespec='seconds')}",
        f"Anomalies: {len(anomalies)}",
    ]
    for day in sorted({a.day for a in anomalies}):
        day_rows = [a for a in anomalies if a.day == day]
        lines += ["", f"## Day {day}", ""]
        lines.append("| Issue | Severity | Signals | Baseline → Current | Change |")
        lines.append("|---|---|---|---|---|")
        for a in day_rows:
            baseline = a.metrics.get("baseline_count", "?")
            current = a.metrics.get("current_count", "?")
            change = f"{a.delta:.0f}%" if a.delta else "new"
            lines.append(
                f"| {a.issue} | {a.severity} | {', '.join(a.signals)} "
                f"| {baseline} → {current} | {change} |"
            )
        for a in day_rows:
            lines += [
                "",
                f"### {a.issue}",
                "",
                a.description,
                "",
                f"**Action:** {a.recommended_action}",
            ]
    if not anomalies:
        lines += ["", "No anomalies detected."]
    return "\n".join(lines) + "\n"
