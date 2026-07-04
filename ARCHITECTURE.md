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
       ConversationAnalysis (JSONB)
                 │
                 ▼
       Relational Projections
      (ConversationIssue, ...)
           │             │
           ▼             ▼
   Anomaly Detection   Knowledge Base
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
active. The in-flight job snapshot is in-memory UI state; a message queue is
not justified at this scale (see the complexity budget).

**Pipeline auditing.** Every stage execution is durably recorded in the
`pipeline_runs` table: which stage ran, when, what triggered it (API or CLI),
how long it took, and how it ended (summary on success, error on failure). A
`running` row is written before the stage executes, so a crashed process
leaves evidence rather than vanishing from history. This audit trail feeds the
stage cards' last-run display, the Recent Runs panel, `GET
/api/pipeline/runs`, and the `app runs` CLI command. Phase 3 LLM calls also
write `llm_call_observations` rows linked to the active run, exposing
per-conversation load, prompt-build, Gemini, persistence, retry, and size
signals through `GET /api/pipeline/llm-observations` and `app bottlenecks`.
Token-usage observability remains planned for the broader Phase 7 evaluation
work.

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

Each conversation is interpreted whole, exactly once, into the canonical
Structured Conversation Object (Version 1, frozen — see
`docs/PHASE3-UNDERSTANDING.md`), containing:

- conversation summaries (short + detailed)
- an `issues[]` array — zero, one, or many issues, each with canonical name,
  the customer's verbatim description, severity, confidence, impact, product,
  symptoms, catalog-match result, and per-issue resolution state
- the overall resolution (type, actions, replacement flag)
- whole-conversation signals (language, emotion, follow-up, confidence)

The Pydantic `StructuredConversation` model owns the structure end to end:
it generates the provider's native structured-output schema, validates every
response (invalid output is retried and never persisted), types the
application code, and is persisted unchanged to
`ConversationAnalysis.analysis_json`. Issues are projected 1:1 into
`conversation_issues`, and the issue catalog is derived from the Day-1
baseline (Days 2–3 normalize against it; unmatched issues surface as
candidate novel issues for anomaly detection). Within a day, a small worker
pool bounds wall-clock time — day boundaries stay strict barriers, so outputs
are identical to sequential processing.

## 3. Structured Conversation Store

Store normalized conversation objects in PostgreSQL.

Embeddings are stored alongside operational data using pgvector.

A single datastore keeps deployment simple while satisfying the scale of this project.

## 4. Anomaly Detection

A **deterministic multi-signal rules engine** (ADR-012) over the relational
projections — the LLM plays no part in detection, and raw conversations are
never reparsed. Each post-baseline day's per-issue statistics (one grouped
SQL query) are compared against Day 1 using four independent signals:

- **volume spike** — frequency rises significantly vs the baseline
- **novel issue** — a category absent from the Day-1 issue catalog
  (anomalous regardless of frequency)
- **severity drift** — the high/critical share changes significantly
- **resolution drift** — the resolved share drops significantly

Signals for the same (day, issue) merge into one **canonical Anomaly** —
issue, derived severity, the signals that fired, the supporting metrics, a
deterministic summary, and a recommended action — persisted in `anomalies`
and regenerated on every run (derived data). Detection thresholds are
explicit settings, so every anomaly explains *why* it was detected rather
than reporting an opaque score.

Slack alerts and the anomaly report **consume anomalies**; they never analyze
operational data themselves. Prompt 3 converts one detected anomaly into a
concise Slack message (deterministic fallback if the LLM fails), delivered to
`SLACK_WEBHOOK_URL` when configured. The report is written to
`reports/anomaly-report.md` and served from the control center.

## 5. Knowledge Base

Only resolved issues become retrieval documents.

Conversation Understanding already performs semantic interpretation.

Phase 5 deliberately avoids a second LLM call.

Instead:

StructuredConversation

↓

KnowledgeDocument

↓

Deterministic knowledge_text rendering

↓

Embedding

KnowledgeDocument becomes the canonical artifact for retrieval.

Embeddings are generated from knowledge_text rather than raw conversations or
JSON documents.

Retrieval first applies deterministic metadata filters before semantic vector
search.

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

- platform-owned taxonomy matching service
- embedding-assisted issue canonicalization
- human taxonomy approval workflow
- cross-encoder reranking
- evaluation datasets
- prompt versioning
- per-AI-call observability (model, latency, token usage — Phase 7, building
  on the `pipeline_runs` audit trail)
- human feedback loops
- automated regression testing
- tool calling
- background workers
- streaming responses
