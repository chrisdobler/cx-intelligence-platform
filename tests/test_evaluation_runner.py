"""DB-backed tests for the evaluation runner (fake provider + fake embedder)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cxintel.config import get_settings
from cxintel.evaluation.service import EvaluationService
from cxintel.knowledge_base.service import KnowledgeBaseService
from cxintel.llm import LLMUsage
from cxintel.models import ConversationAnalysis, ConversationIssue
from cxintel.pipeline.orchestrator import STAGES, get_stage
from cxintel.pipeline.stages import StageKind
from cxintel.repositories import EvaluationRunRepository
from cxintel.resolution_assistant.schema import ResolutionResponse
from cxintel.understanding.schema import StructuredConversation

from .test_knowledge_base import FakeEmbedder, seed_knowledge_scenario
from .test_knowledge_generation import make_issue, make_structured


class FakeProvider:
    """Schema-dispatching provider exposing the Phase 7 usage side-channel."""

    def __init__(self) -> None:
        self.structured = make_structured(
            [make_issue("base water leak")],
            resolved=True,
            resolution_type="replacement",
            requires_replacement=True,
        )
        self.response = ResolutionResponse(
            recommendation="Replace the base seal.",
            reasoning="KB-1 resolved the same leak.",
            recommended_actions=["Ship a replacement base seal."],
            grounded=True,
            evidence_strength="strong",
            citations=["KB-1"],
        )
        self.last_usage = LLMUsage(prompt_tokens=90, output_tokens=10, total_tokens=100)
        self.calls: list[type] = []

    def extract(self, prompt: str, schema: type, on_retry: Any = None) -> Any:
        self.calls.append(schema)
        return self.structured if schema is StructuredConversation else self.response


def _write_golden(root: Path) -> None:
    (root / "understanding").mkdir(parents=True)
    (root / "retrieval").mkdir()
    (root / "resolution").mkdir()
    (root / "dataset.json").write_text(json.dumps({"version": "test-1"}))
    (root / "understanding" / "u1.json").write_text(
        json.dumps(
            {
                "case_id": "u1",
                "description": "leak understanding",
                "messages": [{"role": "customer", "body": "water everywhere"}],
                "expected": {
                    "issues": [{"canonical_name": "base water leak", "severity_in": ["medium"]}],
                    "resolution_resolved": True,
                    "requires_replacement": True,
                },
            }
        )
    )
    issue = {
        "canonical_name": "base water leak",
        "customer_description": "customer says base water leak",
        "severity": "medium",
        "confidence": 0.9,
        "customer_impact": "high",
        "product": "Pod 5",
        "symptoms": ["water pooling under the base"],
        "catalog": {"matched": True, "confidence": 0.9},
        "resolution_status": "unresolved",
        "resolution_summary": None,
    }
    (root / "retrieval" / "r1.json").write_text(
        json.dumps(
            {
                "case_id": "r1",
                "description": "self retrieval",
                "issue": issue,
                "limit": 5,
                "expected_conversation_external_ids": ["kb_leak"],
                "min_recall": 1.0,
            }
        )
    )
    (root / "resolution" / "s1.json").write_text(
        json.dumps(
            {
                "case_id": "s1",
                "description": "grounded replacement",
                "issue": issue,
                "limit": 5,
                "expected": {
                    "grounded": True,
                    "min_citations": 1,
                    "actions_any_keywords": ["replacement"],
                },
            }
        )
    )


@pytest.fixture
def factory(settings_on_test_db: str, migrated_engine: Any, db_session: Session) -> Any:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False)


@pytest.fixture
def eval_env(
    factory: Any, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Seeded KB + golden dataset and report paths under tmp_path."""
    seed_knowledge_scenario(db_session)
    KnowledgeBaseService(factory, FakeEmbedder()).run()
    golden = tmp_path / "golden"
    _write_golden(golden)
    monkeypatch.setenv("EVALUATION_GOLDEN_PATH", str(golden))
    monkeypatch.setenv("EVALUATION_REPORT_PATH", str(tmp_path / "reports" / "evaluation-report"))
    monkeypatch.setenv(
        "EVALUATION_BASELINE_PATH", str(tmp_path / "baseline" / "evaluation-baseline.json")
    )
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_run_produces_report_and_history_row(factory: Any, eval_env: Path) -> None:
    provider = FakeProvider()
    with factory() as session:
        analyses_before = session.execute(
            select(func.count()).select_from(ConversationAnalysis)
        ).scalar_one()
        issues_before = session.execute(
            select(func.count()).select_from(ConversationIssue)
        ).scalar_one()

    result = EvaluationService(factory, provider, FakeEmbedder()).run()
    report = result.report

    assert report.dataset_version == "test-1"
    assert report.total_cases == 3 and report.passed_cases == 3
    assert report.pass_rate == 1.0
    assert report.coverage == {"understanding": 1, "retrieval": 1, "resolution": 1}
    assert report.understanding_prompt_version and report.resolution_prompt_version
    assert report.retrieval_metrics is not None
    assert report.retrieval_metrics.recall_at_k == 1.0
    assert report.grounding_metrics is not None
    assert report.grounding_metrics.citation_validity_rate == 1.0
    # Tokens: understanding + resolution cases (retrieval makes no LLM call).
    assert report.total_tokens == 200
    assert report.baseline is None and report.regressions == []

    json_path = eval_env / "reports" / "evaluation-report.json"
    md_path = eval_env / "reports" / "evaluation-report.md"
    assert json_path.exists() and md_path.exists()
    assert "# Evaluation Report" in md_path.read_text()
    assert result.summary().startswith("Evaluated 3 cases: 3 passed")

    with factory() as session:
        run = EvaluationRunRepository(session).latest()
        assert run is not None
        assert run.status == "succeeded"
        assert run.total_cases == 3 and run.pass_rate == 1.0
        assert run.report["dataset_version"] == "test-1"
        # Production tables untouched — eval never persists analyses/issues.
        assert (
            session.execute(select(func.count()).select_from(ConversationAnalysis)).scalar_one()
            == analyses_before
        )
        assert (
            session.execute(select(func.count()).select_from(ConversationIssue)).scalar_one()
            == issues_before
        )


def test_failing_expectation_fails_case_not_run(factory: Any, eval_env: Path) -> None:
    provider = FakeProvider()
    provider.structured = make_structured(
        [make_issue("something unrelated")], resolved=False, resolution_type=None
    )
    result = EvaluationService(factory, provider, FakeEmbedder()).run(suites=["understanding"])
    report = result.report
    assert report.total_cases == 1 and report.passed_cases == 0
    case = report.cases[0]
    assert not case.passed and case.error is None
    assert any(check.check.endswith(".presence") and not check.passed for check in case.checks)


def test_provider_error_fails_case_with_execution_check(factory: Any, eval_env: Path) -> None:
    class ExplodingProvider(FakeProvider):
        def extract(self, prompt: str, schema: type, on_retry: Any = None) -> Any:
            raise RuntimeError("quota exhausted")

    result = EvaluationService(factory, ExplodingProvider(), FakeEmbedder()).run(
        suites=["understanding"]
    )
    case = result.report.cases[0]
    assert not case.passed
    assert case.error == "quota exhausted"
    assert case.checks[0].kind == "execution"


def test_second_run_detects_regressions_against_promoted_baseline(
    factory: Any, eval_env: Path
) -> None:
    from cxintel.evaluation.report import promote_baseline

    provider = FakeProvider()
    EvaluationService(factory, provider, FakeEmbedder()).run()
    settings = get_settings()
    promote_baseline(
        Path(settings.evaluation_report_path).with_suffix(".json"),
        Path(settings.evaluation_baseline_path),
    )

    provider.structured = make_structured([make_issue("something unrelated")])
    result = EvaluationService(factory, provider, FakeEmbedder()).run()
    report = result.report
    assert report.baseline is not None
    assert any(r.case_id == "u1" for r in report.regressions if r.kind == "case")
    # Informational delta vs the previous evaluation_runs row is present too.
    assert report.previous_run is not None
    assert report.previous_run.pass_rate_before == 1.0

    with factory() as session:
        latest = EvaluationRunRepository(session).latest()
        assert latest is not None and latest.regression_count == len(report.regressions)


def test_evaluate_stage_registered_but_excluded_from_run_remaining() -> None:
    stage = get_stage("evaluate")
    assert stage.kind is StageKind.BATCH
    assert stage.implemented
    assert stage.include_in_run_remaining is False
    # Every other batch stage still participates.
    assert all(
        s.include_in_run_remaining
        for s in STAGES
        if s.key != "evaluate" and s.kind is StageKind.BATCH
    )


def test_broken_dataset_fails_fast_before_any_llm_call(
    factory: Any, eval_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cxintel.evaluation.golden import GoldenDatasetError

    broken = tmp_path / "broken-golden"
    broken.mkdir()
    monkeypatch.setenv("EVALUATION_GOLDEN_PATH", str(broken))
    get_settings.cache_clear()
    provider = FakeProvider()
    with pytest.raises(GoldenDatasetError):
        EvaluationService(factory, provider, FakeEmbedder()).run()
    assert provider.calls == []
