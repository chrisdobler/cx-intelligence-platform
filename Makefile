# Conversation Intelligence Platform — developer tasks.
# Run `make help` (or just `make`) for the list of targets.

.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
# Pin Docker to the LOCAL Docker Desktop context so compose never accidentally
# targets a remote daemon (e.g. an SSH context). Override, e.g.:
#   make up DOCKER_CONTEXT=default
DOCKER_CONTEXT ?= desktop-linux
DOCKER ?= docker --context $(DOCKER_CONTEXT)
COMPOSE ?= $(DOCKER) compose

.PHONY: help install lock up down db-reset db-health fmt lint lint-fix typecheck test check serve clean ingest understand analyze build-kb chat pipeline

help:  ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

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

ingest:  ## [Phase 2] Load and normalise the raw dataset.
	$(UV) run app ingest

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
