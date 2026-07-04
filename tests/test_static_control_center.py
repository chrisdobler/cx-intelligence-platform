"""Static guards for the pipeline control-center page."""

from pathlib import Path


def test_static_control_center_scrolls_to_pipeline_control_on_job_start() -> None:
    html = Path("src/cxintel/api/static/index.html").read_text(encoding="utf-8")

    assert 'id="pipeline-control"' in html
    assert "function scrollToPipelineControl()" in html
    assert 'document.getElementById("pipeline-control")' in html
    assert 'scrollIntoView({ behavior: "smooth", block: "start" })' in html
    assert "await load();\n        scrollToPipelineControl();" in html
