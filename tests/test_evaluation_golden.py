"""Golden dataset loading — the committed cases parse, and broken ones fail loudly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cxintel.evaluation.golden import (
    GoldenDatasetError,
    UnderstandingCase,
    load_golden_dataset,
)

_REPO_GOLDEN = Path(__file__).resolve().parent.parent / "evals" / "golden"


def _write_dataset(root: Path) -> None:
    (root / "understanding").mkdir(parents=True)
    (root / "retrieval").mkdir()
    (root / "resolution").mkdir()
    (root / "dataset.json").write_text(json.dumps({"version": "9.9"}))


def _minimal_case(case_id: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "description": "d",
        "messages": [{"role": "customer", "body": "water everywhere"}],
        "expected": {"issues": [{"canonical_name": "base water leak"}]},
    }


def test_committed_golden_dataset_is_valid() -> None:
    dataset = load_golden_dataset(_REPO_GOLDEN)
    assert dataset.version
    assert dataset.understanding and dataset.retrieval and dataset.resolution
    assert dataset.total_cases == sum(dataset.coverage().values())
    ids = (
        [case.case_id for case in dataset.understanding]
        + [case.case_id for case in dataset.retrieval]
        + [case.case_id for case in dataset.resolution]
    )
    assert len(ids) == len(set(ids))


def test_missing_dataset_stamp_raises(tmp_path: Path) -> None:
    with pytest.raises(GoldenDatasetError, match="not found"):
        load_golden_dataset(tmp_path)


def test_duplicate_case_ids_rejected(tmp_path: Path) -> None:
    _write_dataset(tmp_path)
    for name in ("a.json", "b.json"):
        (tmp_path / "understanding" / name).write_text(json.dumps(_minimal_case("dup-1")))
    with pytest.raises(GoldenDatasetError, match="Duplicate case_id 'dup-1'"):
        load_golden_dataset(tmp_path)


def test_unknown_fields_rejected(tmp_path: Path) -> None:
    _write_dataset(tmp_path)
    case = _minimal_case("u-1")
    case["expected"]["issues"][0]["severty"] = "high"  # typo must not silently no-op
    (tmp_path / "understanding" / "u.json").write_text(json.dumps(case))
    with pytest.raises(GoldenDatasetError, match="Invalid golden case"):
        load_golden_dataset(tmp_path)


def test_invalid_json_rejected(tmp_path: Path) -> None:
    _write_dataset(tmp_path)
    (tmp_path / "retrieval" / "broken.json").write_text("{not json")
    with pytest.raises(GoldenDatasetError, match="Invalid JSON"):
        load_golden_dataset(tmp_path)


def test_cases_load_in_filename_order(tmp_path: Path) -> None:
    _write_dataset(tmp_path)
    (tmp_path / "understanding" / "b-second.json").write_text(json.dumps(_minimal_case("c2")))
    (tmp_path / "understanding" / "a-first.json").write_text(json.dumps(_minimal_case("c1")))
    dataset = load_golden_dataset(tmp_path)
    assert [case.case_id for case in dataset.understanding] == ["c1", "c2"]


def test_understanding_case_requires_messages() -> None:
    case = _minimal_case("u-2")
    case["messages"] = []
    with pytest.raises(ValueError):
        UnderstandingCase.model_validate(case)
