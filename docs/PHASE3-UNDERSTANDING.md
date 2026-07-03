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
