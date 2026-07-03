

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

_Status: Ready for implementation._

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

## Prompt 2 — Resolution Assistant

_Status: Not yet implemented._

This prompt will generate a grounded resolution using retrieved historical conversations.

---

## Prompt 3 — Slack Alert Generation

_Status: Not yet implemented._

This prompt will convert detected anomalies into concise operational Slack alerts.
