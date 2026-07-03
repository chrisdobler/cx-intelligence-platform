# Conversation Intelligence Platform

## Purpose

This project demonstrates a production-oriented AI platform for understanding customer support conversations, detecting emerging operational issues, and assisting support agents through retrieval-augmented generation (RAG).

The emphasis is on engineering judgment rather than model complexity. Every architectural decision should favor simplicity, maintainability, and clear separation of responsibilities.

---

# Design Philosophy

## Guiding Principles

- Prefer the simplest architecture that satisfies the requirements.
- Minimize operational complexity.
- Treat the LLM as one component within a larger pipeline—not the application itself.
- Every stage should be independently testable and rerunnable.
- Favor deterministic processing where possible.
- Produce structured JSON rather than free-form text.
- Keep prompts centralized and versionable.
- Design the pipeline to be idempotent.

---

# High-Level Architecture

```
                 Raw JSON Dataset
                        │
                        ▼
                  Data Ingestion
                        │
                        ▼
          Conversation Understanding
      (summary, issues, metadata extraction)
                        │
                        ▼
          Structured Conversation Object
                        │
                        ▼
        Structured Conversation Store
                 │                 │
                 │                 │
                 ▼                 ▼
        Anomaly Detection     Knowledge Base
                 │                 │
                 ▼                 ▼
          Slack Alerts     Retrieval Pipeline
                                   │
                                   ▼
                           Context Builder
                                   │
                                   ▼
                                LLM
                                   │
                                   ▼
                        Resolution Assistant
```

---

# Shared Pipeline

Both required assignment components build upon the same conversation-understanding pipeline.

A conversation should only be processed once.

The resulting Structured Conversation Object is the central artifact of the platform. Every downstream capability—including analytics, anomaly detection, embeddings, semantic retrieval, and the Resolution Assistant—consumes this normalized representation rather than reprocessing raw conversations.

The resulting structured representation becomes the foundation for:

- anomaly detection
- analytics
- embeddings
- semantic retrieval
- chatbot context

---

# Pipeline Orchestration

Each processing stage — Data Ingestion, Conversation Understanding, Knowledge
Base Generation, Anomaly Detection, and the Resolution Assistant — is exposed
as an **independently executable pipeline job** behind a common interface:

- current status (complete / pending, derived from the data itself)
- prerequisites, each with a human-readable explanation when unmet
- `run()` with progress reporting
- execution metrics and last execution time

A single **orchestration layer** (`cxintel.pipeline`) owns stage definitions,
dependency ordering, and execution. The CLI, the REST API, and the landing-page
control center all invoke this one layer, so business logic is never
duplicated: `app ingest` and clicking **Run** on the Ingestion card execute
exactly the same code path.

Stages are ordered linearly (a valid topological order for this pipeline), but
each stage declares its own explicit prerequisites — Anomaly Detection depends
on Conversation Understanding, not on the Knowledge Base. Stages come in two
kinds: **batch** stages run to completion; **interactive** stages (the
Resolution Assistant) are opened rather than run. Stages whose phase has not
landed yet report themselves as not implemented and cannot be run — the
control center shows them disabled with the planned phase.

**Run Remaining Pipeline** walks the stages in dependency order, skips
anything already complete (stage completion is derived from the data, so
nothing is rerun unnecessarily), executes each runnable incomplete stage, and
stops cleanly with an explanation on reaching a stage that is blocked or not
yet implemented.

Execution is deliberately simple: one in-process background worker runs one
job at a time, and the control center polls the status endpoint while a job is
active. Run state and last-execution records are kept in memory — a message
queue or durable run-history table is not justified at this scale (see the
complexity budget); durable run history is listed under Future Enhancements.

---

# Major Components

## 1. Data Ingestion

Responsibilities:

- Load ticket dataset
- Normalize schema
- Persist conversations
- Be safely rerunnable

## 2. Conversation Understanding

The LLM acts as an information extraction engine.

Expected structured outputs include:

- conversation summary
- primary issue
- secondary issues
- severity
- products involved
- resolution summary
- confidence

## 3. Structured Conversation Store

Store normalized conversation objects in PostgreSQL.

Embeddings are stored alongside operational data using pgvector.

A single datastore keeps deployment simple while satisfying the scale of this project.

## 4. Anomaly Detection

Aggregate issue counts across days.

Identify:

- new issue clusters
- spikes
- accelerating trends
- resolving issues

Generate:

- severity
- magnitude
- Slack alert message

## 5. Knowledge Base

Only resolved conversations become retrieval documents.

Embeddings should be generated from normalized conversation summaries rather than raw conversations whenever practical.

## 6. Resolution Assistant

Pipeline:

1. Understand incoming conversation
2. Retrieve similar resolved conversations
3. Build optimized context
4. Generate grounded resolution path

---

# Technology Decisions

## PostgreSQL + pgvector

Chosen because:

- operational data and vectors naturally belong together
- avoids maintaining two datastores
- simplifies deployment
- sufficient for project scale

If retrieval requirements grow significantly, the vector layer could later be separated without changing the overall architecture.

# AI Provider Strategy

The platform intentionally uses a single AI provider for both language generation and embeddings.

Current implementation:

- Google AI Studio
- Gemini 2.5 Flash (Conversation Understanding and Resolution Assistant)
- gemini-embedding-001 (Knowledge Base embeddings)

Using a single provider minimizes operational complexity, reduces required configuration to a single API key, and keeps the architecture consistent with the project's complexity budget.

The application should start successfully without a configured API key. AI-powered functionality should be gracefully disabled until `GOOGLE_API_KEY` is provided, while infrastructure and developer tooling remain fully available.

---

# Complexity Budget

Every dependency must justify its existence.

The goal is not to maximize technologies used.

The goal is to maximize clarity, maintainability, and engineering quality.

This project intentionally favors a cohesive architecture over distributed complexity.

---

# Future Enhancements

Potential production improvements:

- cross-encoder reranking
- evaluation datasets
- prompt versioning
- audit logging
- human feedback loops
- automated regression testing
- tool calling
- background workers
- durable pipeline run history (runs are tracked in memory today)
- streaming responses
