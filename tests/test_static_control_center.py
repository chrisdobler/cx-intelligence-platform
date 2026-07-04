"""Static guards for the pipeline control-center page."""

from pathlib import Path


def test_static_control_center_scrolls_to_pipeline_control_on_job_start() -> None:
    html = Path("src/cxintel/api/static/index.html").read_text(encoding="utf-8")

    assert 'id="pipeline-control"' in html
    assert "function scrollToPipelineControl()" in html
    assert 'document.getElementById("pipeline-control")' in html
    assert 'scrollIntoView({ behavior: "smooth", block: "start" })' in html
    assert "await load();\n        scrollToPipelineControl();" in html


def test_static_control_center_renders_evaluation_history_dashboard() -> None:
    html = Path("src/cxintel/api/static/index.html").read_text(encoding="utf-8")

    assert 'fetch("/api/evaluation/history?limit=20")' in html
    assert "function overallHistoryChartSVG(history)" in html
    assert "function suiteSparklineSVG(points, label)" in html
    assert "Recent Evaluations" in html
    assert "Compared to Previous Run" in html
    assert "Quality Gates" in html
    assert "Structured output validation" in html
    assert "Retrieval Trend" in html
    assert "Token Usage Trend" in html
