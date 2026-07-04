

# Prompt Library

This document contains the production prompts used by the platform.

Each prompt should include:

- Purpose
- Inputs
- Output Contract (Pydantic)
- Prompt text
- Notes

The prompt is responsible for semantic behavior only.

The structure of every AI-generated artifact is defined by the corresponding
Pydantic model.

Whenever supported by the selected LLM provider, the Pydantic-generated JSON
Schema should be supplied through the provider's native structured-output
mechanism rather than embedded directly into the prompt.

Prompt text should focus on:

- extraction rules
- normalization
- confidence
- evidence
- semantic interpretation

Formatting and structural validation are handled by the schema contract.

---

## Prompt 1 — Conversation Understanding

_Status: Implemented (`prompt_version = "1.2"`, `src/cxintel/understanding/prompt.py`)._

### Purpose

Transform a raw customer support conversation into the canonical
Structured Conversation Object.

Conversation Understanding performs exactly one semantic interpretation of the
conversation.

The output becomes the canonical AI artifact consumed by every downstream
pipeline stage.

---

### Responsibilities

The prompt is responsible for semantic extraction only.

Specifically:

- identify every operational issue discussed
- normalize issue names
- preserve the customer's wording
- extract supporting evidence
- determine confidence
- summarize the conversation
- summarize the resolution

The prompt is **not** responsible for:

- JSON formatting
- output validation
- database persistence
- anomaly detection
- retrieval

Those responsibilities belong to the provider abstraction, Pydantic, and the
pipeline.

---

### Whole Conversation Processing

Conversation Understanding always processes the complete conversation.

Do not chunk conversations.

Do not summarize conversations before extraction.

Do not perform multi-pass extraction.

The complete conversation is interpreted exactly once.

---

### Issue Catalog Normalization

Conversation Understanding receives the current Issue Catalog generated from
the Day 1 baseline.

For each extracted issue:

1. Prefer an existing catalog category whenever it accurately represents the
   customer's problem.

2. Preserve the customer's original wording separately as
   `customer_description`.

3. If no existing catalog category is appropriate, create a new canonical
   issue.

4. Never force an issue into an unrelated existing category.

5. Indicate whether the issue matched an existing catalog entry.

6. For every extracted issue populate:

   - catalog.matched

Canonical issue names should represent broad, stable operational categories
appropriate for reporting and trend analysis.

Avoid creating new canonical categories when an existing category accurately
represents the customer's problem.

Differences in wording, symptoms, firmware revisions, hardware revisions,
product revisions, or troubleshooting state should generally become attributes
of the issue rather than new canonical categories.

The catalog object communicates whether the issue was successfully normalized
against the current operational taxonomy.

Novel issue categories become one of the inputs to the anomaly detection
pipeline.

---

### Output Contract

The output contract is defined exclusively by the corresponding Pydantic model.

Whenever the selected provider supports native structured output, the
Pydantic-generated JSON Schema should be supplied directly to the provider.

The prompt should never duplicate the schema.

The prompt defines semantics.

Pydantic defines structure.

---

### Validation

Every generated response must successfully validate as a
StructuredConversation.

Invalid responses should be retried automatically.

No AI-generated data should be persisted unless validation succeeds.

---

### Prompt Text

The production prompt (assembled by `build_prompt()`; the output schema is
supplied natively via the provider's structured-output mechanism, never
embedded here):

```text
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

{catalog block — either the current Issue Catalog (canonical_name +
description per entry), or, during Day-1 baseline generation, the canonical
names already seen so far}

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

Conversation metadata: product=…, category=…, priority=…, status=…

Conversation transcript:
[customer] …
[agent] …
```

---

## Prompt 2 — Resolution Assistant

_Status: Implemented (`prompt_version = "1.0"`, `src/cxintel/resolution_assistant/prompt.py`)._

### Purpose

Recommend the best resolution for one current issue using only retrieved
historical KnowledgeDocuments — grounded decision support, not autonomous
troubleshooting.

Retrieval, context construction, and citation validation are deterministic
and happen outside this prompt. The assistant never reinterprets the
conversation (that is Prompt #1's single semantic interpretation) and never
performs retrieval.

### Inputs

One `ContextBundle` (`src/cxintel/resolution_assistant/schema.py`) — the
current issue verbatim from the StructuredConversation, the retrieved
KnowledgeDocuments tagged with stable citation ids (`KB-1`, `KB-2`, … in
retrieval rank order), and the retrieval metadata — embedded in the prompt as
JSON data. Payload, not schema.

### Output Contract

`ResolutionResponse{recommendation, reasoning, recommended_actions, grounded,
evidence_strength, citations}` (`src/cxintel/resolution_assistant/schema.py`),
supplied natively via the provider's structured-output mechanism.

### Prompt Text

```text
You are a decision-support assistant for customer-support agents. Retrieval has
already been performed by the platform. Your only knowledge source is the
context bundle below: one current customer issue and a set of historical
knowledge documents describing how similar issues were actually resolved.

Your task:

1. Recommend the single best resolution path for the current issue, using only
   the historical knowledge documents as evidence.
2. Explain briefly WHY the cited evidence supports that recommendation.
3. List concrete, ordered recommended actions. Every action must come from
   actions or resolution summaries in the cited documents — never invent
   troubleshooting steps that no cited document contains.
4. Cite your evidence: citations must be the doc_id values (e.g. "KB-1") of
   the documents that support the recommendation. Cite only documents that
   genuinely support it, not every document supplied.

Grounding rules — these override everything else:

- Use ONLY the knowledge documents in the bundle. Do not use general product
  knowledge, prior training knowledge, or assumptions.
- Do not reinterpret, re-diagnose, or second-guess the current issue; it has
  already been analyzed. Take its fields as given.
- If the retrieved documents are not sufficiently similar to the current
  issue, or contradict each other without a clear best path, set grounded to
  false, state "No sufficiently similar historical resolutions were found."
  as the recommendation, explain why the evidence is insufficient, leave
  recommended_actions empty, and cite nothing. An honest ungrounded answer is
  a successful outcome; an invented recommendation is a failure.
- Set grounded to true only when the recommendation is fully supported by the
  cited documents.

Assess evidence_strength honestly:

- "strong": multiple closely matching documents agree on the resolution.
- "moderate": at least one closely matching document supports it.
- "weak": only partially similar documents support it.
- "none": no usable evidence (grounded must be false).

Context bundle:

{bundle_json}
```

### Notes

When retrieval returns zero documents, the platform answers deterministically
with an ungrounded `ResolutionResponse` and this prompt is never invoked —
"no evidence" must not cost an LLM call.

After every LLM response, citations are validated deterministically against
the supplied bundle: unknown citation ids are dropped, and a response that
claims to be grounded while citing no retrieved document is downgraded to
ungrounded by the platform. Grounding is enforced in code, not trusted to the
model.

---

## Prompt 3 — Slack Alert Generation

_Status: Implemented (`prompt_version = "1.0"`, `src/cxintel/anomaly/prompt.py`)._

### Purpose

Convert one already-detected canonical anomaly into a concise operational
Slack alert. Detection is deterministic (ADR-012) and finished before this
prompt runs — the LLM summarizes findings, it never discovers them.

### Inputs

One `CanonicalAnomaly` (issue, severity, signals, metrics, summary,
recommended action), embedded in the prompt as JSON data — payload, not schema.

### Output Contract

`SlackAlert{text}` (`src/cxintel/anomaly/schema.py`), supplied natively via
the provider's structured-output mechanism.

### Prompt Text

```text
You write concise operational Slack alerts for a customer-support platform.
An anomaly has already been detected by a deterministic rules engine — do not
re-analyze, question, or embellish it. Convert it into a Slack alert:

- 2 to 4 short lines, plain text (Slack markdown like *bold* is fine).
- Lead with the severity and the issue name.
- Include the key numbers from the metrics (counts, percentages).
- End with the recommended action.
- Do not invent facts that are not in the anomaly data.

Detected anomaly:

{anomaly JSON}
```

### Notes

If the provider cannot produce a valid alert, a deterministic fallback
template is used — alert prose can never fail an anomaly-detection run.
