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
make check         # ruff + mypy + pytest — all green, no DB required
make up            # start PostgreSQL + pgvector (waits until healthy)
make db-health     # verify connectivity and that pgvector is installed
make serve         # run the API — then GET http://127.0.0.1:8000/health
make down          # stop the database
```

Optional: copy `.env.example` to `.env` to override any setting.

> **Port already in use?** The database publishes host port 5432 by default. If
> that's taken, pick another: `DB_HOST_PORT=5433 make up` and set
> `DATABASE_URL=postgresql+psycopg://cx:cx@localhost:5433/cx` (in `.env`).

## Project layout

```
src/cxintel/
  config.py          # typed settings (pydantic-settings)
  logging.py         # logging setup
  db.py              # engine, session factory, health check
  cli.py             # `app` CLI (Typer)
  api/app.py         # FastAPI app (/health)
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
