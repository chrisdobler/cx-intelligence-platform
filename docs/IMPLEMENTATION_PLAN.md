# Implementation Plan

This document serves as the execution roadmap for building the Conversation Intelligence Platform. The goal is to complete the project in incremental, independently testable phases.

---

# Guiding Principle

At every stage, prefer:

- working software over perfect software
- simple architecture over clever architecture
- reusable components over duplicated logic
- deterministic outputs over opaque behavior

Each phase should leave the project in a runnable state.

---

# Phase 1 — Foundation

## Objectives

- Initialize project with `uv`
- Configure Python 3.12
- Create project layout
- Configure Ruff, pytest, and typing
- Create Docker Compose for PostgreSQL
- Enable pgvector

Deliverable:

A developer can clone the repository and run the application locally.

---

# Phase 2 — Data Ingestion

## Objectives

- Import the provided JSON dataset
- Normalize conversations
- Persist conversations and messages
- Ensure ingestion is idempotent

Deliverable:

All conversations are available in PostgreSQL.

---

# Phase 3 — Conversation Understanding

> **Status: delivered.** The `StructuredConversation` Pydantic hierarchy,
> Google AI Studio provider (native structured output, validation retries),
> Prompt #1, `ConversationIssue` projection, and Day-1 issue catalog are
> implemented, integrated with the CLI (`app understand [--full]`), the REST
> API, and the control center. One deliberate refinement: conversations are
> still processed whole and exactly once, but within a day a small worker
> pool (default 8) runs extractions concurrently — day boundaries remain
> strict barriers, so outputs are identical to sequential processing. The
> stage exposes explicit **Run Sample (100)** and **Run Full Dataset**
> actions; runs are resumable (already-analyzed conversations are skipped).

## Objectives

Build an LLM extraction pipeline that produces the canonical
Structured Conversation Object for every conversation.

The Conversation Understanding pipeline is the heart of the platform.

Responsibilities:

- Process one conversation at a time.
- Invoke Gemini.
- Produce a Structured Conversation Object.
- Validate the response using Pydantic.
- Persist the canonical JSON unchanged into
  ConversationAnalysis.analysis_json.
- Generate relational projections from that JSON (initially
  ConversationIssue).

The Structured Conversation Object becomes the canonical AI artifact for every
downstream component.

Do not allow downstream components to independently reinterpret raw
conversations.

Deliverable:

Every imported conversation has:

- a validated Structured Conversation Object
- a persisted ConversationAnalysis record
- one or more ConversationIssue projections

These artifacts become the inputs to anomaly detection, retrieval,
evaluation, and future AI capabilities.

---

# Phase 4 — Anomaly Detection

> **Status: delivered.** A deterministic multi-signal rules engine (ADR-012)
> compares each day's issue statistics against the Day-1 baseline — volume
> spikes, novel issues (from the issue catalog), severity drift, and
> resolution drift — and emits canonical anomalies carrying their triggering
> signals, supporting metrics, summary, and recommended action. Slack alerts
> are generated from anomalies via Prompt 3 (deterministic fallback if the
> LLM fails) and delivered when `SLACK_WEBHOOK_URL` is set; the anomaly
> report is written to `reports/anomaly-report.md` and served at
> `GET /api/anomalies/report`. Integrated with `app analyze` / `app report`,
> `GET /api/anomalies`, the control center, and the orchestrator. Thresholds
> are explicit settings (`ANOMALY_SPIKE_THRESHOLD_PCT`,
> `ANOMALY_DRIFT_THRESHOLD`, `ANOMALY_MIN_COUNT`). Raw conversations are
> never reparsed; the LLM plays no part in detection.

## Objectives

Aggregate issue counts across days.

Detect:

- new issue clusters
- spikes
- accelerating trends
- resolving issues

Generate:

- severity
- magnitude
- Slack alert

Deliverable:

Automated anomaly report for Day 2 and Day 3.

---

# Phase 5 — Knowledge Base

## Objectives

- Process resolved conversations
- Generate embeddings
- Store embeddings with pgvector
- Build semantic retrieval

Deliverable:

Relevant historical conversations can be retrieved from semantic search.

---

# Phase 6 — Resolution Assistant

## Objectives

Pipeline:

1. Understand incoming conversation
2. Retrieve similar conversations
3. Build optimized context
4. Generate grounded resolution

Deliverable:

Interactive assistant capable of suggesting resolution paths.

---

# Phase 7 — Evaluation

## Objectives

Evaluate:

- retrieval quality
- prompt quality
- response quality
- anomaly detection quality

Record:

- prompt version
- model
- latency
- token usage
- retrieved documents
- generated response

Deliverable:

Basic evaluation and observability framework.

---

# Phase 8 — Developer Experience & Pipeline Control Center

## Objectives

Make the platform immediately understandable, easy to demonstrate, and
**operable from the landing page**. After `git clone`, a reviewer runs one
command, opens one URL, and can discover *and drive* the whole platform
without reading documentation.

- One-command lifecycle: `make start` (DB + Adminer + API, prints the URLs) and
  `make stop`.
- The application should remain usable even before AI is configured. Infrastructure services (PostgreSQL, pgvector, Adminer, FastAPI, API documentation, and the landing page) must start successfully without a `GOOGLE_API_KEY`.

### Pipeline control center

The landing page at `/` is the operational control center, not a passive
dashboard (single static file served by FastAPI — no front-end framework,
respecting the complexity budget):

- **Service Status** — green/yellow/red for PostgreSQL, pgvector, API.
- **Stage cards** — one card per pipeline stage (Data Ingestion, Conversation
  Understanding, Anomaly Detection, Knowledge Base, Resolution Assistant),
  each showing: name, short description, status, prerequisites (with
  explanations when unmet), outputs produced, last execution time, and a
  primary action button (Run / Run Again / Open).
- **Run Remaining Pipeline** — a prominent top-level action that executes
  every incomplete stage in dependency order, skipping completed stages, and
  stopping cleanly with an explanation at the first blocked or
  not-yet-implemented stage.
- **Stage dependency handling** — stages whose prerequisites are unmet are
  disabled and explain why; stages whose phase has not landed are disabled
  with the planned phase. Completion is derived from the data, so nothing is
  rerun unnecessarily.
- **Quick Actions / Quick Links** — API docs, database UI, health, config.

### Orchestration

- A single orchestration layer (`cxintel.pipeline`) defines the stages, their
  prerequisites, and execution. The CLI (`app ingest`, `app pipeline`), the
  REST API (`POST /api/pipeline/{stage}/run`, `POST /api/pipeline/run`), and
  the landing page all call this one layer — no duplicated business logic.
- Background execution: one in-process worker, one job at a time; the page
  polls status while a job runs.
- **Pipeline auditing**: every stage execution is durably recorded in the
  `pipeline_runs` table (stage, trigger source, timing, outcome; a `running`
  row is written up front so crashes leave evidence). Surfaced via the Recent
  Runs panel, `GET /api/pipeline/runs`, and `app runs`. Phase 3 understanding
  now records per-conversation `llm_call_observations` for load, prompt, LLM,
  persistence, retry, and size bottleneck analysis, surfaced via
  `GET /api/pipeline/llm-observations` and `app bottlenecks`; token usage
  remains part of the broader Phase 7 observability work.

### Onboarding / AI setup

- The landing page detects whether `GOOGLE_API_KEY` is configured. If missing,
  an **Enable AI Capabilities** card explains that infrastructure works
  without a key and which AI stages are disabled, links to Google AI Studio,
  and accepts the key through a secure password-style input with **Save
  Configuration** — written only to the local `.env`, never echoed back, and
  applied live (no restart needed).
- Once the key is saved, AI-stage prerequisites flip to met automatically.

### Miscellaneous

- **Adminer** in Docker Compose so the database is inspectable with no external
  tools.
- Typed JSON status surface (`/api/status`, `/api/config`): the stage cards,
  job state, and headline counts all come from one payload; later phases light
  their stages up by implementing the stage's `run()` — the page never needs
  redesigning.
- Keep the simple commands working (`uv sync`, `uv run app pipeline`,
  `uv run app chat`); README, architecture docs, and a reproducible environment.

Deliverable:

`git clone … && make start`, open http://localhost:8000, and the whole platform
is discoverable — and runnable — from the control center.

---

# Stretch Goals

If time permits:

- reranking
- tool calling
- streaming responses
- background workers
- regression datasets
- prompt versioning
- audit dashboard
