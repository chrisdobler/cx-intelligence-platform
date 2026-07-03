"""Prompt #3 — Slack Alert Generation (see docs/PROMPT_LIBRARY.md).

A deliberately tiny prompt: detection is already done. Its only job is to
convert one canonical anomaly (data payload, not schema) into a concise
operational Slack message. Output structure is owned by the
:class:`~cxintel.anomaly.schema.SlackAlert` Pydantic model, supplied natively
to the provider.
"""

from __future__ import annotations

from .schema import CanonicalAnomaly

PROMPT_VERSION = "1.0"

_INSTRUCTIONS = """\
You write concise operational Slack alerts for a customer-support platform.
An anomaly has already been detected by a deterministic rules engine — do not
re-analyze, question, or embellish it. Convert it into a Slack alert:

- 2 to 4 short lines, plain text (Slack markdown like *bold* is fine).
- Lead with the severity and the issue name.
- Include the key numbers from the metrics (counts, percentages).
- End with the recommended action.
- Do not invent facts that are not in the anomaly data.

Detected anomaly:

{anomaly_json}
"""


def build_slack_prompt(anomaly: CanonicalAnomaly) -> str:
    """Assemble Prompt #3 for one detected anomaly."""
    return _INSTRUCTIONS.format(anomaly_json=anomaly.model_dump_json(indent=2))


def fallback_slack_message(anomaly: CanonicalAnomaly) -> str:
    """Deterministic alert used when the LLM cannot produce one."""
    signals = ", ".join(s.value for s in anomaly.signals)
    return (
        f"[{anomaly.severity.upper()}] {anomaly.issue} (day {anomaly.day}; {signals})\n"
        f"{anomaly.summary}\n"
        f"Action: {anomaly.recommended_action}"
    )
