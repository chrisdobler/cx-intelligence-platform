"""Deterministic knowledge_text rendering — the exact text that gets embedded.

A plain natural-language template over one KnowledgeDocument. Embeddings are
generated from this text only — never from raw conversations or JSON
documents. Empty sections are omitted so the embedding carries no filler.
"""

from __future__ import annotations

from .schema import KnowledgeDocument


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _section(label: str, value: str) -> str:
    return f"{label}:\n{value}"


def _bullet_section(label: str, values: list[str], *, skip: set[str]) -> str | None:
    bullets: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if not item or item in seen or item in skip:
            continue
        seen.add(item)
        bullets.append(f"- {item}")
    if not bullets:
        return None
    return f"{label}:\n" + "\n".join(bullets)


def render_knowledge_text(doc: KnowledgeDocument) -> str:
    """Render one KnowledgeDocument as retrieval-optimized natural language."""
    issue = _clean(doc.issue)
    customer_description = _clean(doc.customer_description)
    resolution_summary = _clean(doc.resolution_summary)
    resolution_type = _clean(doc.resolution_type)
    outcome = _clean(doc.outcome)

    sections = [
        _section("Problem", issue),
    ]
    scalar_values = {issue, resolution_summary, outcome}
    if customer_description:
        sections.append(_section("Customer reported", customer_description))
        scalar_values.add(customer_description)
    sections.append(_section("Resolution", resolution_summary))
    if resolution_type:
        sections.append(_section("Resolution type", resolution_type))
        scalar_values.add(resolution_type)
    sections.append(_section("Outcome", outcome))

    symptoms = _bullet_section("Symptoms", doc.symptoms, skip=scalar_values)
    if symptoms:
        sections.append(symptoms)
    actions = _bullet_section("Support actions", doc.actions, skip=scalar_values)
    if actions:
        sections.append(actions)
    return "\n\n".join(sections)
