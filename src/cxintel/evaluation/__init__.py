"""Phase 7 — deterministic evaluation of the platform's AI stages (ADR-015).

The golden dataset (``evals/golden/``) holds curated inputs with expected
canonical artifacts; the evaluation runner executes them through the real
production code paths and compares structured outputs field by field. LLMs
are the system under evaluation — never the evaluation framework.
"""
