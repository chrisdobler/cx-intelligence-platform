# Phase 5 — Knowledge Base

## Goal

Transform resolved operational experience into reusable knowledge suitable for
semantic retrieval.

Unlike previous phases, this stage does not interpret conversations.

It transforms existing structured knowledge into retrieval artifacts.

Pipeline:

Conversation
        │
        ▼
StructuredConversation
        │
        ▼
KnowledgeDocument
        │
        ▼
knowledge_text
        │
        ▼
Embedding
        │
        ▼
pgvector

---

## Design Philosophy

The LLM has already performed semantic reasoning during Conversation
Understanding.

Phase 5 intentionally avoids a second LLM call.

Knowledge generation is deterministic.

Only embedding generation uses an AI model.

---

## KnowledgeDocument

One resolved Issue produces one KnowledgeDocument.

KnowledgeDocuments are generated only from successfully resolved issues.

Unresolved conversations are intentionally excluded from the knowledge base.

Suggested schema:

- issue
- customer_description
- product
- symptoms
- prerequisites
- resolution_type
- resolution_summary
- actions
- outcome

KnowledgeDocument is a Pydantic model.

It is derived entirely from StructuredConversation.

---

## knowledge_text

Embeddings are generated from a deterministic natural-language rendering of
KnowledgeDocument.

Example structure:

Problem

Customer reported

Resolution

Resolution Type

Outcome

Symptoms

Support actions

The rendering omits standalone metadata such as product because retrieval uses
that field as a deterministic metadata filter before vector search.

The application generates this text using a template.

The LLM is not used.

---

## Retrieval Strategy

Retrieval is performed in two stages.

Stage 1:

Metadata filtering.

Examples:

- product
- resolved only

Stage 2:

Semantic retrieval using pgvector.

If metadata filtering produces no candidates, the retrieval layer may
progressively relax metadata constraints before performing semantic search.

---

## Phase 5 Deliverable

At the end of Phase 5:

StructuredConversation

↓

KnowledgeDocument

↓

knowledge_text

↓

Embedding

↓

pgvector

should operate deterministically without requiring an additional LLM prompt.
