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

PROMPT_VERSION = "1.2"

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

Treat issue extraction as operational classification rather than issue naming.

Your objective is to classify customer problems into fewer, broader, stable
operational reporting categories while preserving every distinct operational
issue the customer experienced.

The Issue Catalog represents the organization's operational taxonomy.

Your responsibility is to maintain the consistency of that taxonomy.

You are performing operational classification, not inventing user-facing
labels.

Your goal is to reduce taxonomy fragmentation and minimize unnecessary
category proliferation while accurately representing distinct operational
problems.

Assume an existing category is correct unless there is strong evidence that
the customer's issue represents a genuinely different operational problem.

For every issue:

- Populate catalog.matched.
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
- broad enough for reporting and trend analysis
- independent of customer wording whenever practical

Differences in wording, symptoms, firmware revisions, hardware revisions,
product revisions, or troubleshooting state should generally become attributes
of an issue rather than new canonical issue names, unless those differences
represent genuinely different operational problems.

If you are uncertain whether an issue belongs to an existing category or a
new category, prefer the existing category.

Examples

The following customer descriptions should normalize to the same operational
category:

"The left side of my Pod gets extremely hot."

"The mattress gets too warm after about an hour."

"Temperature fluctuates throughout the night."

"The Pod overheats during sleep."

→ canonical_name:

pod overheating

--------------------------------

"The hub disconnects from WiFi."

"The Pod keeps losing network connectivity."

"The hub repeatedly goes offline."

"The app cannot stay connected to the Pod."

→ canonical_name:

intermittent connectivity

--------------------------------

"Charged twice."

"Duplicate subscription charge."

"Unexpected renewal."

"I was billed for a subscription I already cancelled."

→ canonical_name:

incorrect billing charge

--------------------------------

"Water is leaking from the base."

"The base has a crack and fluid is coming out."

"There is moisture under the Pod base."

"The base reservoir is leaking onto the floor."

→ canonical_name:

base water leak

--------------------------------

"I need a replacement unit."

"Support said they would send a new hub."

"The replacement Pod never arrived."

"Can you replace the defective base?"

→ canonical_name:

replacement request
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
