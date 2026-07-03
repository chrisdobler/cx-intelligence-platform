# Phase 4 — Anomaly Detection

## Goal

Transform normalized operational data into actionable operational intelligence.

Unlike Phase 3, this stage does **not** consume raw conversations.

It operates entirely on relational projections generated during Conversation
Understanding.

Pipeline:

ConversationIssue
        │
        ▼
Issue Catalog
        │
        ▼
Operational Statistics
        │
        ▼
Anomaly Detection
        │
        ▼
Canonical Anomaly
        │
        ├──────────────┐
        ▼              ▼
Slack Alert      Reports

---

## Inputs

- ConversationIssue
- IssueCatalog
- Conversation

Raw conversations should never be reparsed during this phase.

---

## Canonical Anomaly

Every detected anomaly should be represented by a canonical object.

Suggested schema:

```json
{
  "issue": "Pod Overheating",

  "severity": "critical",

  "signals": [
    "volume_spike",
    "novel_issue"
  ],

  "metrics": {
    "baseline_count": 31,
    "current_count": 97,
    "percent_change": 213
  },

  "summary": "...",

  "recommended_action": "..."
}
```

The anomaly object becomes the canonical artifact consumed by Slack alerts,
reports, dashboards, and future workflows.

---

## Anomaly Signals

Version 1 should detect anomalies using multiple independent signals.

### Volume Spike

Known issue frequency increases significantly compared to the Day 1 baseline.

### Novel Issue

An extracted issue does not match the Issue Catalog generated from Day 1.

Novel issue categories are considered anomalies regardless of frequency.

### Severity Drift

Issue frequency remains stable while operational severity changes
significantly.

### Resolution Drift

Resolution patterns change significantly (for example, a large increase in
replacement requests).

---

## Detection Philosophy

The platform should explain *why* an issue is anomalous rather than relying on
a single opaque score.

Every anomaly should identify the signals that caused it to be detected.

---

## Slack Alerts

Slack alerts are generated only after anomalies have been detected.

Conversation Understanding should never generate Slack alerts directly.

The anomaly object becomes the input to a small LLM prompt responsible only for
summarizing operational findings into human-readable alerts.
