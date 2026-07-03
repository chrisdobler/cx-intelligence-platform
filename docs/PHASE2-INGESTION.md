

# Phase 2 — Data Ingestion

## Goal

Build the data ingestion pipeline that imports the provided customer support dataset into PostgreSQL. This phase intentionally contains **no AI logic**. The objective is to establish a clean, reliable data foundation for the Conversation Understanding pipeline.

---

## Objectives

### 1. Database Schema

Create the initial database schema using SQLAlchemy models and Alembic migrations.

The schema should distinguish between:

- Raw source data (immutable)
- AI-generated analysis (derived)

Initial tables:

### Conversation

Represents the canonical support conversation imported from the source dataset.

Suggested fields:

- id (UUID)
- external_id
- customer_id
- status
- priority
- category (only if present in the source dataset)
- issue_type (only if present in the source dataset)
- product (only if present in the source dataset)
- started_at
- ended_at
- created_at
- updated_at

This table should contain only source data—not AI-generated information.

### Message

Represents an individual message within a conversation.

Suggested fields:

- id
- conversation_id (FK)
- role
- body
- created_at

### ConversationAnalysis

Stores AI-generated understanding of a conversation.

Suggested fields:

- conversation_id (FK)
- model
- model_version
- prompt_version
- processed_at
- analysis_json (JSONB)

The `analysis_json` column will contain the canonical Structured Conversation Object.

Using JSONB allows the AI schema to evolve without requiring database migrations whenever new fields are introduced.

### Anomaly

Stores detected operational anomalies.

Suggested fields:

- id
- day
- issue
- severity
- delta
- description
- slack_message
- created_at

Do **not** create embedding tables yet. Embeddings belong to Phase 5.

---

### 2. Ingestion Pipeline

Implement a seed/import pipeline that:

- Loads `sample_tickets_v6.json`
- Validates the input
- Creates Conversation objects
- Creates Message objects
- Persists everything to PostgreSQL

The pipeline should be idempotent so it can be safely rerun.

Expose this through:

- `app ingest`
- `make ingest`

---

### 3. Repository Layer

Create repository classes to isolate persistence from business logic.

Suggested repositories:

- ConversationRepository
- MessageRepository
- ConversationAnalysisRepository
- AnomalyRepository

Services should interact with repositories rather than SQLAlchemy sessions directly.

---

### 4. Service Layer

Create an `IngestionService` responsible for orchestrating the import process.

Desired flow:

JSON → IngestionService → Repositories → PostgreSQL

Keep business logic separate from persistence.

### 4.1 Data Ownership

Maintain a strict separation between raw data and AI-derived data.

Rules:

- Source dataset fields belong in `Conversation` and `Message`.
- AI-generated fields belong exclusively in `ConversationAnalysis.analysis_json`.
- Business services should never modify imported source data.
- AI processing should produce a new Structured Conversation Object rather than mutating the original conversation.

---

### 5. Verification

Add a command such as:

`app stats`

that reports:

- total conversations
- total messages
- resolved conversations
- pending conversations
- escalated conversations
- dataset date range

This command should verify ingestion completed successfully.

---

## Out of Scope

Do not implement:

- Gemini integration
- Conversation Understanding
- Embeddings
- pgvector usage
- Anomaly detection
- Resolution Assistant

Those belong to later phases.

---

## Deliverable

At the end of Phase 2, a developer should be able to:

1. Run the database migrations.
2. Import the dataset.
3. Inspect the imported data in Adminer.
4. Verify the import using `app stats`.

This establishes the canonical data foundation for all subsequent AI pipeline stages.