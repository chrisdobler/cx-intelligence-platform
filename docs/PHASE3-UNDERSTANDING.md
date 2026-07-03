# Canonical AI Artifact

The Conversation Understanding pipeline produces a single canonical
**Structured Conversation Object** for every conversation.

This object is the authoritative AI interpretation of the conversation and is
stored without modification in:

    ConversationAnalysis.analysis_json

All downstream processing should derive from this canonical artifact.

The JSON representation is never edited manually.

Future prompt improvements should regenerate this object rather than mutate it.

---

## Relational Projections

Downstream systems should not repeatedly parse JSONB.

Instead, specialized relational projections should be generated from the
Structured Conversation Object.

Initial projection:

ConversationIssue

Future projections may include:

- Resolution Knowledge
- Product Analytics
- Customer Sentiment

This allows the AI schema to evolve independently while providing efficient SQL
queries for analytics and reporting.

---

## ConversationIssue

Represents a single issue extracted from a conversation.

One conversation may produce zero, one, or many issues.

Suggested schema:

- id
- conversation_id (FK)
- canonical_name
- customer_description
- severity
- confidence
- created_at

This table is **derived** from the Structured Conversation Object.

It should never be edited manually.

Its purpose is to support:

- anomaly detection
- analytics
- reporting
- future retrieval strategies

---

## Processing Pipeline

Conversation
        │
        ▼
Conversation Understanding (Gemini)
        │
        ▼
Structured Conversation Object
        │
        ├──────────────┐
        ▼              ▼
ConversationAnalysis   ConversationIssue
    (JSONB)            (Projection)

ConversationAnalysis remains the canonical AI artifact.

ConversationIssue is a relational projection optimized for querying.

---

# Canonical Structured Conversation Object

> **Status:** Version 1 (Frozen)
>
> Version 1 of the Structured Conversation Object is considered complete.
>
> The remainder of Phase 3 should treat this schema as the canonical contract
> between Conversation Understanding and every downstream component.
>
> Future improvements should extend this schema rather than redesign it.

The objective of Version 1 is not to capture every possible attribute.

Instead, Version 1 defines the stable contract required by every downstream
consumer of the Conversation Understanding pipeline.

Future enhancements should extend this schema while preserving backward
compatibility whenever practical.

The Conversation Understanding pipeline should produce a single canonical
Structured Conversation Object for every conversation.

This object represents the authoritative AI understanding of the conversation
and is persisted unchanged in `ConversationAnalysis.analysis_json`.

All downstream systems—including anomaly detection, retrieval, evaluation,
analytics, and future AI capabilities—consume this object either directly or
through relational projections.

Suggested schema:

```json
{
  "summary": {
    "short": "...",
    "detailed": "..."
  },

  "issues": [
    {
      "canonical_name": "...",
      "customer_description": "...",
      "severity": "high",
      "confidence": 0.96,
      "customer_impact": "high",
      "product": "Pod 5",
      "symptoms": [
        "..."
      ],
      "catalog": {
        "matched": true,
        "confidence": 0.98
      },
      "resolution_status": "resolved",
      "resolution_summary": "..."
    }
  ],

  "resolution": {
    "resolved": true,
    "resolution_type": "replacement",
    "summary": "...",
    "actions": [
      "..."
    ],
    "requires_replacement": false
  },

  "conversation": {
    "language": "English",
    "multiple_issues": true,
    "requires_followup": false,
    "customer_emotion": "frustrated",
    "analysis_confidence": 0.94
  }
}
```

## Design Principles

The schema is intentionally designed around downstream consumers rather than
everything an LLM could potentially extract.

Every field should have a clear consumer.

The fundamental analytical unit of the platform is the **Issue**, not the
Conversation.

Conversations provide context.

Issues provide operational intelligence.

One conversation may legitimately contain zero, one, or many issues.

Examples:

| Field | Primary Consumer |
|-------|-------------------|
| canonical_name | Anomaly Detection, Analytics |
| customer_description | Embeddings, Retrieval |
| severity | Prioritization |
| customer_impact | Reporting |
| product | Analytics |
| symptoms | Future Retrieval |
| catalog.matched | Issue Catalog / Anomaly Detection |
| catalog.confidence | Taxonomy Quality |
| resolution_summary | Knowledge Base |
| resolution_type | Analytics |
| analysis_confidence | Evaluation |

The schema should evolve conservatively. New fields may be added without
database migrations because the object is stored as JSONB.

The ConversationIssue relational projection should be generated directly from
the `issues` array contained within this object.

The projection should preserve a one-to-one relationship between each Issue in
the Structured Conversation Object and each row in ConversationIssue.

---

## Phase 3 Remaining Work

With the schema finalized, the remainder of Phase 3 should focus on
implementation rather than further architectural changes.

Remaining tasks:

1. Implement the Pydantic models representing the Structured Conversation
   Object.
2. Generate the JSON Schema from the Pydantic models.
3. Design Prompt #1 (Conversation Understanding).
4. Integrate Gemini 2.5 Flash using native structured output.
5. Validate Gemini output using Pydantic.
6. Persist the canonical JSON to `ConversationAnalysis`.
7. Generate the `ConversationIssue` relational projection.
8. Add retry handling for malformed or invalid structured responses.

The objective is to produce one validated Structured Conversation Object for
every imported conversation.

Once these tasks are complete, Phase 3 should be considered architecturally
complete. Future work should build upon the Structured Conversation Object
rather than redefining it.

---

## Structured Output Contract

Conversation Understanding should never consume raw JSON directly.

Instead, the pipeline follows the sequence:

```
Conversation
        │
        ▼
LLM Provider
        │
        ▼
Structured Output
        │
        ▼
Pydantic Validation
        │
        ▼
StructuredConversation
        │
        ▼
ConversationAnalysis (JSONB)
        │
        ▼
ConversationIssue
```

The `StructuredConversation` Pydantic model is the canonical contract between
the LLM and the remainder of the platform.

Whenever the selected provider supports native structured output, the
Pydantic-generated JSON Schema should be supplied directly to the provider.

The application should never duplicate the schema inside prompts.

Responsibilities are intentionally separated:

### Prompt

Defines extraction behavior:

- issue normalization
- confidence scoring
- evidence extraction
- resolution extraction
- semantic interpretation

### Pydantic

Defines output structure:

- required fields
- types
- nesting
- validation
- schema generation

### LLM Provider

Responsible for translating the provider-specific structured-output API into
the canonical `StructuredConversation` model.

This abstraction keeps the remainder of the platform provider-agnostic while
allowing Google AI Studio, OpenAI, Anthropic, or future providers to implement
structured output using their native capabilities.

No AI-generated data should ever be persisted until it successfully validates
against the Pydantic model.

---

## Canonical Artifact Principle

The Structured Conversation Object is the single source of truth produced by
Conversation Understanding.

No downstream component should reinterpret raw conversations.

Instead:

Conversation
        │
        ▼
Conversation Understanding
        │
        ▼
Structured Conversation Object
        │
        ├──────────────┐
        │              │
        ▼              ▼
ConversationIssue   Future Projections
        │
        ▼
Analytics / RAG / Evaluation

Every downstream relational model is a projection generated from the canonical
Structured Conversation Object.

If the prompt or model changes, projections should be regenerated rather than
manually updated.

This keeps AI evolution isolated from the operational schema while ensuring
every consumer observes a consistent interpretation of each conversation.

---

## Relational Projection Strategy

The Structured Conversation Object is the canonical AI artifact.

Relational tables should be treated as projections generated from that artifact.

The initial projection is:

### ConversationIssue

Each Issue contained within the Structured Conversation Object should produce
exactly one ConversationIssue record.

The projection exists solely to optimize querying and analytics.

The projection should never become the source of truth.

Instead:

Conversation
        │
        ▼
Conversation Understanding
        │
        ▼
ConversationAnalysis (JSONB)
        │
        ▼
ConversationIssue

Future projections may include:

- Product analytics
- Resolution analytics
- Customer sentiment
- Reporting views

All projections should be regenerated from the canonical Structured
Conversation Object whenever Conversation Understanding is rerun.

Downstream systems should consume relational projections rather than repeatedly
parsing JSONB.

---

## Baseline Issue Catalog

Conversation Understanding also produces a second derived artifact:

**IssueCatalog**

Unlike `ConversationIssue`, which represents issues extracted from individual
conversations, the Issue Catalog represents the platform's current taxonomy of
known issue categories.

For this project, the catalog is generated **exclusively from Day 1**, which
the assignment defines as the operational baseline.

Pipeline:

```
Day 1 Conversations
        │
        ▼
Conversation Understanding
        │
        ▼
ConversationIssue
        │
        ▼
IssueCatalog
```

The catalog should contain one entry for every canonical issue discovered in
the Day 1 baseline.

Suggested fields:

- canonical_name
- description
- first_seen_day
- example_count
- representative_examples

The catalog is derived data and may be regenerated at any time.

It is **not** manually maintained.

---

## Day 2 and Day 3 Processing

For conversations processed after the baseline:

```
Conversation
        │
        ▼
Conversation Understanding
        │
        ▼
ConversationIssue
        │
        ▼
IssueCatalog Matching
```

Each extracted issue should attempt to match an existing Issue Catalog entry.

When no suitable category exists, the issue should be treated as a **candidate
novel issue** rather than being forced into an existing category.

Novel issue categories become one of the signals used by the anomaly detection
pipeline.

This complements traditional volume-based anomaly detection by identifying
entirely new operational problems rather than only increases in known ones.

---

## ConversationIssue Projection

Each Issue should contain sufficient information to serve as the platform's
primary analytical unit.

Version 1 should include:

- canonical_name
- customer_description
- severity
- confidence
- customer_impact
- product
- symptoms
- catalog
- resolution_status
- resolution_summary

Future versions may extend this projection without changing the canonical
Structured Conversation Object.
