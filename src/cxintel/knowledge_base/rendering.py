"""Deterministic knowledge_text rendering — the exact text that gets embedded.

A plain natural-language template over one KnowledgeDocument. Embeddings are
generated from this text only — never from raw conversations or JSON
documents. Empty sections are omitted so the embedding carries no filler.
"""

from __future__ import annotations

from .schema import KnowledgeDocument


def render_knowledge_text(doc: KnowledgeDocument) -> str:
    """Render one KnowledgeDocument as retrieval-optimized natural language."""
    lines = [f"Problem: {doc.issue}."]
    if doc.product:
        lines.append(f"Product: {doc.product}.")
    if doc.symptoms:
        lines.append(f"Symptoms: {'; '.join(doc.symptoms)}.")
    if doc.prerequisites:
        lines.append(f"Diagnostics performed: {'; '.join(doc.prerequisites)}.")
    lines.append(f"Resolution: {doc.resolution_summary}.")
    if doc.actions:
        lines.append(f"Actions taken: {'; '.join(doc.actions)}.")
    if doc.resolution_type:
        lines.append(f"Resolution type: {doc.resolution_type}.")
    lines.append(f"Outcome: {doc.outcome}.")
    return "\n".join(lines)
