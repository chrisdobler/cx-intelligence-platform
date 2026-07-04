"""Prompt #1 — Conversation Understanding (see docs/PROMPT_LIBRARY.md).

The prompt carries semantics only: extraction rules, normalization, confidence,
evidence, interpretation. Output structure is owned entirely by the
StructuredConversation Pydantic schema, supplied to the provider natively —
never embedded here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Conversation, IssueCatalogEntry, Message

PROMPT_VERSION = "1.1"

_INSTRUCTIONS = """\
You are an information extraction engine for a customer support platform.
Analyze the complete support conversation below and extract its structured
interpretation. You are extracting facts, not making business decisions.

Extraction rules:

1. Identify EVERY distinct operational issue the customer experienced. A
   conversation may contain zero, one, or many issues. Do not merge distinct
   problems into one issue; do not invent issues that are not discussed.
2. For each issue, choose a short lowercase canonical_name for the stable
   operational issue category (e.g. "pod overheating" or "base water leak"),
   and preserve the customer's own wording verbatim in customer_description.
3. Extract concrete symptoms as evidence — quote or closely paraphrase the
   conversation. Never fabricate evidence.
4. Score confidence honestly on a 0-1 scale: how certain you are that the
   issue was correctly identified and categorized. Use analysis_confidence
   for your overall confidence in the whole analysis.
5. Assess severity (operational seriousness — safety hazards are critical)
   and customer_impact (how strongly the customer's use of the product is
   affected) independently.
6. Summarize the conversation (short: one sentence; detailed: a few
   sentences) and the resolution: whether it was resolved, what type of
   resolution, the concrete actions taken, and whether a hardware
   replacement is still outstanding.

Issue catalog normalization:

{catalog_block}

Treat issue extraction as a classification task rather than a naming task.

Your objective is to classify customer problems into stable operational
reporting categories.

For every issue:

- Populate catalog.matched and catalog.confidence.
- Reuse an existing catalog category whenever it accurately represents the
  customer's problem.
- Reuse the catalog's exact canonical_name when matched.
- Preserve the customer's original wording separately as
  customer_description.
- Avoid creating a new canonical category when an existing category is an
  appropriate fit.
- Create a new canonical category only when no existing category accurately
  represents the customer's issue.
- Never force an issue into an unrelated category.

Canonical issue names should be:

- short
- lowercase
- stable over time
- appropriate for reporting and analytics
- independent of customer wording whenever practical

Differences in wording, symptoms, firmware revisions, or product revisions
should generally become attributes of an issue rather than new canonical issue
names, unless those differences represent genuinely different operational
problems.
"""

_EMPTY_CATALOG = """\
The issue catalog is currently empty (baseline generation in progress).
Choose clear, reusable canonical names; reuse a name from the "already seen"
list below when it accurately describes the problem.

Canonical names already seen in this baseline:
{names}\
"""

_CATALOG = """\
The current issue catalog (the platform's known issue taxonomy):
{entries}\
"""


def _catalog_block(
    catalog: list[IssueCatalogEntry], seen_names: list[str] | None = None
) -> str:
    if catalog:
        entries = "\n".join(f"- {e.canonical_name}: {e.description}" for e in catalog)
        return _CATALOG.format(entries=entries)
    names = "\n".join(f"- {n}" for n in (seen_names or [])) or "- (none yet)"
    return _EMPTY_CATALOG.format(names=names)


def build_prompt(
    conversation: Conversation,
    messages: list[Message],
    catalog: list[IssueCatalogEntry],
    seen_names: list[str] | None = None,
) -> str:
    """Assemble Prompt #1 for one whole conversation (no chunking — ADR-010)."""
    transcript = "\n".join(f"[{m.role}] {m.body}" for m in messages)
    return (
        _INSTRUCTIONS.format(catalog_block=_catalog_block(catalog, seen_names))
        + "\n"
        + f"Conversation metadata: product={conversation.product}, "
        + f"category={conversation.category}, priority={conversation.priority}, "
        + f"status={conversation.status}\n\n"
        + "Conversation transcript:\n"
        + transcript
    )
