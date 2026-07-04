"""Evaluation runner — executes the golden dataset through production code paths.

Each suite exercises the exact machinery it evaluates:

- understanding: Prompt #1 (``understanding.prompt.build_prompt``) over
  transient ``Conversation``/``Message`` stand-ins (never persisted),
- retrieval: ``render_issue_query`` + the Phase 5 ``retrieve()``,
- resolution: ``build_context`` → Prompt #2 → ``validate_citations`` with the
  same zero-hit guard as production.

Nothing is written to production tables — the only persistence is the
``evaluation_runs`` history row (best-effort, like the other observability
writes) and the report files. Per-case LLM failures fail that case, never the
run.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from ..knowledge_base.retrieval import retrieve
from ..llm import EmbeddingProvider, LLMProvider, LLMUsage
from ..models import Conversation, EvaluationRun, Message
from ..pipeline.progress import ProgressCallback, ProgressReporter
from ..repositories import (
    ConversationRepository,
    EvaluationRunRepository,
    IssueCatalogRepository,
    KnowledgeDocumentRepository,
)
from ..resolution_assistant.context import (
    build_context,
    render_issue_query,
    ungrounded_response,
    validate_citations,
)
from ..resolution_assistant.prompt import PROMPT_VERSION as RESOLUTION_PROMPT_VERSION
from ..resolution_assistant.prompt import build_resolution_prompt
from ..resolution_assistant.schema import ResolutionResponse
from ..understanding.prompt import PROMPT_VERSION as UNDERSTANDING_PROMPT_VERSION
from ..understanding.prompt import build_prompt
from ..understanding.schema import StructuredConversation
from .comparison import (
    CaseResult,
    TokenUsageSummary,
    compare_resolution,
    compare_understanding,
    execution_failure,
    score_retrieval,
)
from .golden import (
    SUITES,
    GoldenDataset,
    ResolutionCase,
    RetrievalCase,
    SuiteName,
    UnderstandingCase,
    load_golden_dataset,
)
from .metrics import aggregate_grounding, aggregate_retrieval
from .report import (
    BaselineRef,
    EvaluationReport,
    PreviousRunDelta,
    detect_regressions,
    load_baseline,
    render_markdown,
    summarize_suite,
)

logger = logging.getLogger(__name__)

GoldenCase = UnderstandingCase | RetrievalCase | ResolutionCase

_SUITE_OF: dict[type, SuiteName] = {
    UnderstandingCase: "understanding",
    RetrievalCase: "retrieval",
    ResolutionCase: "resolution",
}


def _noop_progress(_message: object) -> None:
    return None


class EvaluationResult:
    """Outcome of one evaluation run."""

    def __init__(self, report: EvaluationReport, report_path: Path) -> None:
        self.report = report
        self.report_path = report_path

    def summary(self) -> str:
        report = self.report
        parts = [
            f"Evaluated {report.total_cases} cases: {report.passed_cases} passed "
            f"({report.pass_rate * 100:.1f}%)."
        ]
        if report.baseline is None:
            parts.append("No baseline promoted — regressions not evaluated.")
        else:
            parts.append(f"{len(report.regressions)} regressions vs baseline.")
        parts.append(f"Report: {self.report_path}.")
        return " ".join(parts)


class EvaluationService:
    """Runs the golden dataset and produces the deterministic evaluation report."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: LLMProvider,
        embedder: EmbeddingProvider,
        pipeline_run_id: uuid.UUID | None = None,
    ) -> None:
        from ..config import get_settings

        settings = get_settings()
        self._session_factory = session_factory
        self._provider = provider
        self._embedder = embedder
        self._pipeline_run_id = pipeline_run_id
        self._model = settings.llm_model
        self._embedding_model = settings.embedding_model
        self._golden_root = Path(settings.evaluation_golden_path)
        self._report_base = Path(settings.evaluation_report_path)
        self._baseline_path = Path(settings.evaluation_baseline_path)

    def run(
        self,
        suites: list[str] | None = None,
        progress: ProgressCallback | ProgressReporter = _noop_progress,
    ) -> EvaluationResult:
        """Execute the golden dataset (all suites by default) and write the report."""
        reporter = (
            progress
            if isinstance(progress, ProgressReporter)
            else ProgressReporter(
                stage_key="evaluate",
                stage_label="Evaluation",
                progress=progress,
                message="Loading golden dataset…",
            )
        )
        started_at = datetime.now(tz=UTC)
        started = time.perf_counter()
        selected = list(suites) if suites else list(SUITES)

        # Fail fast on a broken dataset — before any LLM call.
        dataset = load_golden_dataset(self._golden_root)

        plan: list[GoldenCase] = []
        if "understanding" in selected:
            plan += dataset.understanding
        if "retrieval" in selected:
            plan += dataset.retrieval
        if "resolution" in selected:
            plan += dataset.resolution

        reporter.report(
            total_work=len(plan),
            completed_work=0,
            succeeded_work=0,
            message=f"Evaluating {len(plan)} golden cases…",
        )

        results: dict[str, list[CaseResult]] = {suite: [] for suite in SUITES}
        for case in plan:
            reporter.set_current(case.case_id, message=f"Evaluating {case.case_id}…")
            result = self._run_case(case)
            results[result.suite].append(result)
            reporter.advance(current_item=case.case_id, failed=not result.passed)

        report = self._build_report(dataset, selected, results, started_at, started)
        report_path = self._write_report(report)
        self._record_run(report, started_at)
        return EvaluationResult(report, report_path)

    # -- per-case execution ---------------------------------------------------

    def _run_case(self, case: GoldenCase) -> CaseResult:
        case_started = time.perf_counter()
        try:
            if isinstance(case, UnderstandingCase):
                result = self._run_understanding_case(case)
            elif isinstance(case, RetrievalCase):
                result = self._run_retrieval_case(case)
            else:
                result = self._run_resolution_case(case)
        except Exception as exc:  # LLM/infra failure fails the case, not the run
            logger.warning("evaluation case %s failed to execute: %s", case.case_id, exc)
            result = CaseResult(
                case_id=case.case_id,
                suite=_SUITE_OF[type(case)],
                description=case.description,
                passed=False,
                checks=[execution_failure(str(exc))],
                error=str(exc),
            )
        result.duration_seconds = time.perf_counter() - case_started
        return result

    def _case_tokens(self) -> TokenUsageSummary | None:
        usage: LLMUsage | None = getattr(self._provider, "last_usage", None)
        if usage is None:
            return None
        return TokenUsageSummary(
            prompt_tokens=usage.prompt_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        )

    def _run_understanding_case(self, case: UnderstandingCase) -> CaseResult:
        # Transient stand-ins for Prompt #1 — never added to a session, never
        # persisted (same pattern as ResolutionAssistantService._structure_ticket).
        conversation = Conversation(
            product=case.conversation.product,
            category=case.conversation.category,
            priority=case.conversation.priority,
            status=case.conversation.status,
        )
        messages = [Message(role=m.role, body=m.body) for m in case.messages]
        with self._session_factory() as session:
            catalog = IssueCatalogRepository(session).all()
        prompt = build_prompt(conversation, messages, catalog)
        actual: StructuredConversation = self._provider.extract(prompt, StructuredConversation)
        checks = compare_understanding(actual, case.expected)
        return CaseResult(
            case_id=case.case_id,
            suite="understanding",
            description=case.description,
            passed=all(check.passed for check in checks),
            checks=checks,
            tokens=self._case_tokens(),
        )

    def _run_retrieval_case(self, case: RetrievalCase) -> CaseResult:
        query = render_issue_query(case.issue)
        product = case.issue.product or None
        with self._session_factory() as session:
            hits = retrieve(session, self._embedder, query, product=product, limit=case.limit)
            external_ids = ConversationRepository(session).external_ids_by_ids(
                [hit.conversation_id for hit in hits]
            )
            kb_external_ids = KnowledgeDocumentRepository(session).source_external_ids()
        retrieved = [
            external_ids.get(hit.conversation_id, str(hit.conversation_id)) for hit in hits
        ]
        filter_relaxed = product is not None and any(hit.product != product for hit in hits)
        checks, metrics = score_retrieval(
            retrieved,
            case.expected_conversation_external_ids,
            min_recall=case.min_recall,
            min_precision=case.min_precision,
            expect_filter_relaxed=case.expect_filter_relaxed,
            filter_relaxed=filter_relaxed,
            kb_external_ids=kb_external_ids,
        )
        return CaseResult(
            case_id=case.case_id,
            suite="retrieval",
            description=case.description,
            passed=all(check.passed for check in checks),
            checks=checks,
            metrics=metrics,
        )

    def _run_resolution_case(self, case: ResolutionCase) -> CaseResult:
        with self._session_factory() as session:
            bundle = build_context(session, self._embedder, case.issue, limit=case.limit)
            external_ids = ConversationRepository(session).external_ids_by_ids(
                [doc.conversation_id for doc in bundle.documents]
            )
        tokens: TokenUsageSummary | None = None
        if not bundle.documents:
            # The production zero-hit guard: deterministic, no LLM call.
            raw = ungrounded_response(
                "The knowledge base returned no documents for this issue "
                f"(query: {bundle.retrieval.query_text!r}). "
                "A grounded recommendation is not possible."
            )
            validated = raw
        else:
            raw = self._provider.extract(build_resolution_prompt(bundle), ResolutionResponse)
            validated = validate_citations(raw, bundle)
            tokens = self._case_tokens()
        checks, metrics = compare_resolution(raw, validated, bundle, case.expected, external_ids)
        return CaseResult(
            case_id=case.case_id,
            suite="resolution",
            description=case.description,
            passed=all(check.passed for check in checks),
            checks=checks,
            tokens=tokens,
            metrics=metrics,
        )

    # -- report assembly and persistence ---------------------------------------

    def _build_report(
        self,
        dataset: GoldenDataset,
        selected: list[str],
        results: dict[str, list[CaseResult]],
        started_at: datetime,
        started: float,
    ) -> EvaluationReport:
        all_cases = [case for suite in SUITES for case in results[suite]]
        token_totals = [
            case.tokens.total_tokens
            for case in all_cases
            if case.tokens is not None and case.tokens.total_tokens is not None
        ]
        report = EvaluationReport(
            run_id=uuid.uuid4(),
            generated_at=datetime.now(tz=UTC),
            duration_seconds=time.perf_counter() - started,
            dataset_version=dataset.version,
            suites_run=selected,
            model=self._model,
            embedding_model=self._embedding_model,
            understanding_prompt_version=UNDERSTANDING_PROMPT_VERSION,
            resolution_prompt_version=RESOLUTION_PROMPT_VERSION,
            coverage=dataset.coverage(),
            summary={suite: summarize_suite(results[suite]) for suite in selected},
            retrieval_metrics=aggregate_retrieval(results["retrieval"]),
            grounding_metrics=aggregate_grounding(results["resolution"]),
            total_tokens=sum(token_totals) if token_totals else None,
            cases=all_cases,
        )

        baseline = load_baseline(self._baseline_path)
        if baseline is not None:
            regressions, improvements = detect_regressions(report, baseline)
            report.baseline = BaselineRef(
                path=str(self._baseline_path),
                generated_at=baseline.generated_at,
                dataset_version=baseline.dataset_version,
                model=baseline.model,
            )
            report.regressions = regressions
            report.improvements = improvements
            if baseline.dataset_version != report.dataset_version:
                logger.info(
                    "baseline dataset v%s differs from current v%s — "
                    "regressions compared on common case ids only",
                    baseline.dataset_version,
                    report.dataset_version,
                )

        report.previous_run = self._previous_run_delta(report, started_at)
        return report

    def _previous_run_delta(
        self, report: EvaluationReport, started_at: datetime
    ) -> PreviousRunDelta | None:
        """Informational pass-rate delta against the previous evaluation_runs row."""
        try:
            with self._session_factory() as session:
                previous = EvaluationRunRepository(session).latest()
                if previous is None or previous.started_at >= started_at:
                    return None
                return PreviousRunDelta(
                    finished_at=previous.finished_at,
                    pass_rate_before=previous.pass_rate,
                    pass_rate_after=report.pass_rate,
                )
        except Exception as exc:
            logger.warning("failed to read previous evaluation run: %s", exc)
            return None

    def _write_report(self, report: EvaluationReport) -> Path:
        json_path = self._report_base.with_suffix(".json")
        md_path = self._report_base.with_suffix(".md")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(report.model_dump_json(indent=2) + "\n")
        md_path.write_text(render_markdown(report))
        return md_path

    def _record_run(self, report: EvaluationReport, started_at: datetime) -> None:
        """Persist the history row without making it a hard dependency."""
        try:
            with self._session_factory() as session:
                EvaluationRunRepository(session).add(
                    EvaluationRun(
                        id=report.run_id,
                        pipeline_run_id=self._pipeline_run_id,
                        dataset_version=report.dataset_version,
                        model=report.model,
                        embedding_model=report.embedding_model,
                        understanding_prompt_version=report.understanding_prompt_version,
                        resolution_prompt_version=report.resolution_prompt_version,
                        suites=report.suites_run,
                        status="succeeded",
                        total_cases=report.total_cases,
                        passed_cases=report.passed_cases,
                        pass_rate=report.pass_rate,
                        regression_count=len(report.regressions),
                        retrieval_metrics=(
                            report.retrieval_metrics.model_dump()
                            if report.retrieval_metrics
                            else None
                        ),
                        grounding_metrics=(
                            report.grounding_metrics.model_dump()
                            if report.grounding_metrics
                            else None
                        ),
                        total_tokens=report.total_tokens,
                        report=report.model_dump(mode="json"),
                        started_at=started_at,
                        finished_at=report.generated_at,
                        duration_seconds=report.duration_seconds,
                        error=None,
                    )
                )
                session.commit()
        except Exception as exc:
            logger.warning("failed to record evaluation run: %s", exc)
