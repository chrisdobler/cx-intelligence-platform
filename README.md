# Conversation Intelligence Platform

A production-oriented pipeline for understanding customer-support conversations,
detecting emerging operational issues, and assisting agents through
retrieval-augmented generation (RAG).

- **Design:** [`ARCHITECTURE.md`](ARCHITECTURE.md)
- **Plan / status:** [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)

> **Status: Phase 1 (Foundation) complete.** This is a runnable skeleton —
> tooling, database infrastructure, configuration, and the CLI/API shells.
> No pipeline logic yet; every stage command is a stub. See the plan for the
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

Optional: copy `.env.example` to `.env` to override any setting.

> **Port already in use?** The database publishes host port 5432 and Adminer
> 8080 by default. Override either: `DB_HOST_PORT=5433 ADMINER_HOST_PORT=8081
> make start` (and set `DATABASE_URL=postgresql+psycopg://cx:cx@localhost:5433/cx`
> in `.env` if you moved the DB port).

## Control center

The landing page at [http://localhost:8000](http://localhost:8000) is the
control center for the platform:

- **Service Status** — green/yellow/red health for PostgreSQL, pgvector, and the API.
- **Pipeline Status** — progress across the five processing stages (placeholders
  until each phase lands) plus headline counts.
- **Quick Actions / Links** — jump to the API docs, the database UI, and more.

Endpoints:

| Path | Purpose |
|---|---|
| `/` | Control-center landing page |
| `/docs` | Swagger UI |
| `/health` | Machine health probe (JSON) |
| `/api/status` | Service + pipeline status (backs the landing page) |
| `/api/config` | Non-secret configuration (secrets reported only as set/unset) |
| `http://localhost:8080` | Adminer database UI (server `db`, user/pass/db all `cx`) |

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
| `fmt` | Format with Ruff |
| `lint` / `lint-fix` | Lint (and auto-fix) with Ruff |
| `typecheck` | Type-check with mypy (strict) |
| `test` | Run pytest |
| `check` | `lint` + `typecheck` + `test` (CI gate) |
| `serve` | Run the FastAPI service |
| `clean` | Remove caches and build artifacts |
| `ingest` / `understand` / `analyze` / `build-kb` / `chat` / `pipeline` | Stage passthroughs (Phase 2–8 stubs) |

## CLI (`app`)

```bash
app --help
app version
app db health
app serve
# Stubs until their phase:
app ingest | app understand | app analyze | app build-kb | app chat | app pipeline
```

## Configuration

All settings come from environment variables (or `.env`); see `.env.example`.
Key ones: `DATABASE_URL`, `DB_HOST_PORT`, `ANTHROPIC_API_KEY`, `LLM_MODEL`,
`EMBEDDING_PROVIDER`, `SLACK_WEBHOOK_URL`, `UNDERSTAND_LIMIT`, `LOG_LEVEL`.
