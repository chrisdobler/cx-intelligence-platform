# Conversation Intelligence Platform — developer tasks.
# Run `make help` (or just `make`) for the list of targets.

.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
DATA_ARTIFACTS_BUNDLE ?= data/processed/data-artifacts.tgz

# Docker context to target. Empty (the committed default) uses whatever context
# is currently active — portable, works on any machine with a working Docker.
# If you juggle multiple contexts (e.g. a remote SSH daemon) and want to pin
# THIS project to one, set DOCKER_CONTEXT without editing this file:
#   - per command:  make up DOCKER_CONTEXT=desktop-linux
#   - your shell:    export DOCKER_CONTEXT=desktop-linux
#   - persistently:  create a git-ignored Makefile.local containing:
#                        DOCKER_CONTEXT := desktop-linux
-include Makefile.local
DOCKER_CONTEXT ?=
DOCKER := docker $(if $(strip $(DOCKER_CONTEXT)),--context $(strip $(DOCKER_CONTEXT)))
COMPOSE := $(DOCKER) compose

.PHONY: help start stop install lock up down db-reset db-health db-migrate fmt lint lint-fix typecheck test check serve clean data-artifacts backup-artifacts ingest stats understand analyze build-kb chat pipeline .ensure-api-port-available

help:  ## Show this help.
	@grep -h -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

start: .ensure-api-port-available  ## Start the full stack (DB + Adminer + API) and open the control center.
	$(COMPOSE) up -d --wait
	@api_port="$$( $(UV) run python -c 'from cxintel.config import get_settings; print(get_settings().api_port)' )"; \
	echo ""; \
	echo "  Conversation Intelligence Platform is starting."; \
	echo "  ─────────────────────────────────────────────"; \
	echo "  Control center   http://localhost:$$api_port"; \
	echo "  API docs         http://localhost:$$api_port/docs"; \
	echo "  Database UI      http://localhost:8080   (auto-login to cx)"; \
	echo "  ─────────────────────────────────────────────"; \
	echo "  Serving the API (Ctrl-C to stop it, then 'make stop' for the containers)."; \
	echo ""
	$(UV) run app serve

.ensure-api-port-available:
	@set -e; \
	api_port="$$( $(UV) run python -c 'from cxintel.config import get_settings; print(get_settings().api_port)' )"; \
	listeners="$$(lsof -nP -iTCP:"$$api_port" -sTCP:LISTEN -t 2>/dev/null | sort -u || true)"; \
	if [ -z "$$listeners" ]; then \
		exit 0; \
	fi; \
	echo ""; \
	echo "  API port $$api_port is already in use."; \
	echo ""; \
	lsof -nP -iTCP:"$$api_port" -sTCP:LISTEN || true; \
	echo ""; \
	if [ ! -t 0 ]; then \
		echo "  Non-interactive shell; not killing listener(s). Stop them or set API_PORT." >&2; \
		exit 1; \
	fi; \
	read -r -p "  Kill listener(s) and restart the API? [y/N] " answer; \
	case "$$answer" in \
		y|Y|yes|YES) ;; \
		*) echo "  Aborting. Existing listener(s) left running."; exit 1 ;; \
	esac; \
	for pid in $$listeners; do \
		echo "  Stopping PID $$pid"; \
		kill "$$pid" 2>/dev/null || true; \
	done; \
	for _ in 1 2 3 4 5 6 7 8 9 10; do \
		remaining="$$(lsof -nP -iTCP:"$$api_port" -sTCP:LISTEN -t 2>/dev/null | sort -u || true)"; \
		[ -z "$$remaining" ] && exit 0; \
		sleep 0.5; \
	done; \
	echo "  Port $$api_port is still in use after stopping:" >&2; \
	lsof -nP -iTCP:"$$api_port" -sTCP:LISTEN >&2 || true; \
	exit 1

stop:  ## Stop the containers (DB + Adminer).
	$(COMPOSE) down

install:  ## Create the virtualenv and install all dependencies (uv sync).
	$(UV) sync

lock:  ## Update the uv lockfile.
	$(UV) lock

up:  ## Start PostgreSQL + pgvector (docker compose, detached).
	$(COMPOSE) up -d --wait

down:  ## Stop the database containers.
	$(COMPOSE) down

db-reset:  ## Recreate the database from scratch (drops the volume).
	$(COMPOSE) down -v
	$(COMPOSE) up -d --wait

db-health:  ## Check database connectivity and pgvector availability.
	$(UV) run app db health

db-migrate:  ## Apply database migrations.
	$(UV) run alembic upgrade head

fmt:  ## Format the code with Ruff.
	$(UV) run ruff format .

lint:  ## Lint the code with Ruff.
	$(UV) run ruff check .

lint-fix:  ## Lint and auto-fix with Ruff.
	$(UV) run ruff check --fix .

typecheck:  ## Type-check with mypy.
	$(UV) run mypy

test:  ## Run the test suite.
	$(UV) run pytest

check: lint typecheck test  ## Run lint, type-check, and tests (CI gate).

serve:  ## Run the FastAPI service.
	$(UV) run app serve

clean:  ## Remove caches and build artifacts.
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build .coverage htmlcov
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +

data-artifacts: up  ## Refresh data/processed/data-artifacts.tgz with derived AI artifacts only.
	@set -euo pipefail; \
	out="$(DATA_ARTIFACTS_BUNDLE)"; \
	staging="$$(mktemp -d /tmp/cxintel-data-artifacts.XXXXXX)"; \
	cleanup() { rm -rf "$$staging"; }; \
	trap cleanup EXIT; \
	mkdir -p "$$(dirname "$$out")"; \
	$(UV) run app export-derived "$$staging/derived-ai-dataset.zip"; \
	( \
		cd "$$staging"; \
		shasum -a 256 derived-ai-dataset.zip > SHA256SUMS; \
		{ \
			echo "Derived data artifact bundle: $$out"; \
			echo "Contents include derived-ai-dataset.zip and SHA256SUMS."; \
			echo "Import derived AI data from repo root: app import-derived $$out"; \
			echo "Inspect: tar -tzf $$out | head"; \
			echo "Extract: mkdir -p /tmp/cxintel-artifacts && tar -xzf $$out -C /tmp/cxintel-artifacts"; \
		} > RESTORE.md; \
	); \
	tar -czf "$$out" -C "$$staging" .; \
	shasum -a 256 "$$out" > "$$out.sha256"; \
	{ \
		echo "Derived data artifact bundle: $$out"; \
		echo "Checksum: $$out.sha256"; \
		echo "Import derived AI data: app import-derived $$out"; \
		echo "Inspect: tar -tzf $$out | head"; \
		echo "Extract: mkdir -p /tmp/cxintel-artifacts && tar -xzf $$out -C /tmp/cxintel-artifacts"; \
	} > "$$(dirname "$$out")/data-artifacts.RESTORE.md"; \
	tar -tzf "$$out" >/dev/null; \
	echo "Created and verified $$out"

backup-artifacts: data-artifacts

ingest:  ## [Phase 2] Load and normalise the raw dataset.
	$(UV) run app ingest

stats:  ## [Phase 2] Show ingestion statistics.
	$(UV) run app stats

understand:  ## [Phase 3] Run LLM conversation understanding.
	$(UV) run app understand

analyze:  ## [Phase 4] Detect anomalies and emit Slack alerts.
	$(UV) run app analyze

build-kb:  ## [Phase 5] Build the retrieval knowledge base.
	$(UV) run app build-kb

chat:  ## [Phase 6] Interactive resolution assistant.
	$(UV) run app chat

pipeline:  ## [Phase 8] Run the full ingest -> understand -> build-kb pipeline.
	$(UV) run app pipeline
