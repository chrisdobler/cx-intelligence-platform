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

## Objectives

Build an LLM extraction pipeline that produces structured JSON for every conversation.

Extract:

- summary
- primary issue
- secondary issues
- severity
- products
- resolution summary
- confidence

Persist the results back into PostgreSQL.

Deliverable:

Every conversation has a normalized AI-generated representation.

---

# Phase 4 — Anomaly Detection

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

# Phase 8 — Developer Experience

## Objectives

Make the platform immediately understandable and easy to demonstrate. After
`git clone`, a reviewer runs one command, opens one URL, and discovers
everything without reading documentation.

- One-command lifecycle: `make start` (DB + Adminer + API, prints the URLs) and
  `make stop`.
- The application should remain usable even before AI is configured. Infrastructure services (PostgreSQL, pgvector, Adminer, FastAPI, API documentation, and the landing page) must start successfully without a `GOOGLE_API_KEY`.
- The landing page should detect whether `GOOGLE_API_KEY` is configured. If it is missing, present a short onboarding flow that explains how to obtain a free Google AI Studio API key and which AI capabilities are currently disabled.
- Once the key is configured and the application is restarted, Conversation Understanding, Knowledge Base generation, and the Resolution Assistant should become available without any additional configuration.
- A minimal **control-center landing page** at `/`, served by FastAPI from a
  single static file (no front-end framework — respects the complexity budget):
  - **Service Status** — green/yellow/red for PostgreSQL, pgvector, API.
  - **Pipeline Status** — the five processing stages plus headline counts.
  - **Quick Actions / Quick Links** — API docs, database UI, health, config.
- **Adminer** in Docker Compose so the database is inspectable with no external
  tools.
- Typed JSON status surface (`/api/status`, `/api/config`) structured so later
  phases populate live counts (imported / processed / embeddings / anomalies)
  by editing `api/status.py` only — the page never needs redesigning.
- Keep the simple commands working (`uv sync`, `uv run app pipeline`,
  `uv run app chat`); README, architecture docs, and a reproducible environment.

- The landing page should detect whether `GOOGLE_API_KEY` is configured and guide first-time users through enabling AI capabilities without preventing the rest of the platform from running.

Deliverable:

`git clone … && make start`, open http://localhost:8000, and the whole platform
is discoverable from the control center.

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