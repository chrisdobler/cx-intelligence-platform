"""Pipeline orchestration: stages, orchestrator, and background job execution.

The single entry point for running pipeline stages — the CLI, the REST API,
and the landing-page control center all invoke this layer so business logic
is never duplicated.
"""
