# Conversation Intelligence Platform

A production-oriented pipeline for understanding customer-support conversations,
detecting emerging operational issues, and assisting agents through
retrieval-augmented generation (RAG).

- **Design:** [`ARCHITECTURE.md`](ARCHITECTURE.md)
- **Plan / status:** [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)

> **Status: Phase 2 (Data Ingestion) complete, with pipeline orchestration.**
> The dataset can be ingested idempotently, and every pipeline stage is
> exposed as an independent job runnable from the landing page, the CLI, or
> the REST API. The AI stages (understanding, anomaly detection, knowledge
> base, resolution assistant) land in Phases 3–6. See the plan for the
> phase roadmap.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python packaging; installs Python 3.12 for you)
- Docker + Docker Compose (for PostgreSQL + pgvector)
- `make`

## Quickstart

```bash
make install       # create the venv and install dependencies (uv sync)
make start         # start the database + Adminer, then serve the app
# → open http://localhost:8000
make stop          # stop the containers (Ctrl-C stops the API first)
```


`make start` brings up PostgreSQL + pgvector (waiting until healthy) and the
Adminer database UI, then serves the API in the foreground and prints the URLs.
Everything is discoverable from the landing page — no further docs required.

If Google AI Studio has not yet been configured, the landing page detects the missing `GOOGLE_API_KEY` and shows an **Enable AI Capabilities** card: create a free key at [Google AI Studio](https://aistudio.google.com/apikey), paste it into the card, and click **Save Configuration** — the key is written to your local `.env` and AI capabilities enable immediately, no restart or manual file editing required. The database, Adminer, API, and documentation remain fully usable without the key; only AI-powered capabilities are disabled until it is configured.

Optional: copy `.env.example` to `.env` to override any setting.

> **Port already in use?** If the API port is already occupied, `make start`
> will show the listener and ask whether to stop it before restarting the API.
> Set `API_PORT=8001` if you prefer to serve the API on a different port.
> The database publishes host port 5432 and Adminer 8080 by default. Override
> either: `DB_HOST_PORT=5433 ADMINER_HOST_PORT=8081 make start` (and set
> `DATABASE_URL=postgresql+psycopg://cx:cx@localhost:5433/cx` in `.env` if you
> moved the DB port).
>
> Adminer auto-logs into the local dev database. Set `ADMINER_AUTOLOGIN=0` in
> `.env` if you want the normal login form instead.

## Control center

The landing page at [http://localhost:8000](http://localhost:8000) is the
operational control center for the platform — the pipeline is run from here,
not just observed:

- **Service Status** — green/yellow/red health for PostgreSQL, pgvector, and the API.
- **Pipeline stage cards** — one card per stage (Data Ingestion, Conversation
  Understanding, Anomaly Detection, Knowledge Base, Resolution Assistant)
  showing its status, prerequisites, outputs, and last execution, with a
  **Run** / **Run Again** / **Open** action. Stages whose prerequisites are
  unmet are disabled and explain why; stages from future phases are disabled
  with their planned phase.
- **Run Remaining Pipeline** — one click executes every incomplete stage in
  dependency order, skipping completed stages (completion is derived from the
  data, so nothing reruns unnecessarily) and stopping cleanly at the first
  blocked or not-yet-implemented stage.
- **Recent Runs** — the pipeline audit trail: every stage execution is
  durably recorded (stage, trigger source, timing, outcome — including
  failures) and the latest runs are shown here. Also available via
  `GET /api/pipeline/runs` and `app runs`.
- **AI onboarding** — if `GOOGLE_API_KEY` is missing, the Enable AI
  Capabilities card accepts a key via a password-style input and saves it to
  your local `.env` (never echoed back); AI-stage prerequisites flip to met
  immediately.
- **Quick Actions / Links** — jump to the API docs, the database UI, and more.

The CLI, the REST API, and the landing page all drive the same orchestration
layer — `app ingest` and the Ingestion card's Run button execute the same code.

Endpoints:

| Path | Purpose |
|---|---|
| `/` | Control-center landing page |
| `/docs` | Swagger UI |
| `/health` | Machine health probe (JSON) |
| `/api/status` | Services, AI, stage cards, job state, metrics (backs the landing page) |
| `POST /api/pipeline/run` | Run every incomplete pipeline stage in dependency order |
| `POST /api/pipeline/{stage}/run` | Run a single pipeline stage in the background |
| `GET /api/pipeline/runs` | Pipeline audit trail — recent stage runs, newest first |
| `/api/config` | Non-secret configuration (secrets reported only as set/unset) |
| `POST /api/config/google-key` | Save the Google AI Studio key from the onboarding card |
| `http://localhost:8080` | Adminer database UI (auto-login to dev database `cx`) |

### Lower-level targets

`make start`/`stop` are built on smaller targets you can also run directly:
`make up`/`down` (containers only), `make serve` (API only), `make db-health`.

## Project layout

```
src/cxintel/
  config.py          # typed settings (pydantic-settings)
  logging.py         # logging setup
  db.py              # engine, session factory, health check
  cli.py             # `app` CLI (Typer)
  api/app.py         # FastAPI app (landing page + /health, /api/status, /api/config)
  api/status.py      # typed platform-status model (backs the control center)
  api/static/        # control-center landing page
  ingestion/         # Phase 2  (placeholder)
  understanding/     # Phase 3  (placeholder)
  resolution_assistant/ # Phase 6 (placeholder)
  knowledge_base/    # Phase 5  (placeholder)
  anomaly/           # Phase 4  (placeholder)
tests/               # foundation + API smoke tests
docker/Dockerfile    # pgvector image + baked-in init scripts
docker/initdb/       # pgvector init script (runs on DB first boot)
data/raw/            # place sample_tickets_v6.json here (git-ignored)
```

## Make targets

| Target | Description |
|---|---|
| `start` / `stop` | Start the full stack (DB + Adminer + API) / stop the containers |
| `install` | Create the venv and install all dependencies (`uv sync`) |
| `lock` | Update the uv lockfile |
| `up` / `down` | Start / stop PostgreSQL + pgvector |
| `db-reset` | Recreate the database (drops the volume) |
| `db-health` | Check DB connectivity + pgvector |
| `db-migrate` | Apply database migrations |
| `fmt` | Format with Ruff |
| `lint` / `lint-fix` | Lint (and auto-fix) with Ruff |
| `typecheck` | Type-check with mypy (strict) |
| `test` | Run pytest |
| `check` | `lint` + `typecheck` + `test` (CI gate) |
| `serve` | Run the FastAPI service |
| `clean` | Remove caches and build artifacts |
| `ingest` / `stats` / `pipeline` | Import the dataset / show ingestion stats / run remaining stages |
| `understand` / `analyze` / `build-kb` / `chat` | Stage passthroughs (stubs until Phases 3–6) |

## CLI (`app`)

```bash
app --help
app version
app db health | app db upgrade
app serve
app ingest         # import the dataset (idempotent; applies migrations first)
app stats          # ingestion statistics — verifies the import
app pipeline       # run every incomplete pipeline stage in dependency order
app runs           # pipeline audit trail — recent stage runs, newest first
# Stubs until their phase:
app understand | app analyze | app build-kb | app chat
```

## Configuration

All settings come from environment variables (or `.env`); see `.env.example`.
Key ones: `GOOGLE_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL`, `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `DATABASE_URL`, `SLACK_WEBHOOK_URL`, `LOG_LEVEL`.
