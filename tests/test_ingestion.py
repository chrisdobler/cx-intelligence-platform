"""Tests for the ingestion pipeline: loader validation and pure transforms."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cxintel.ingestion.loader import load_raw_conversations


def make_record(
    conversation_id: str = "conv_0001",
    status: str = "resolved",
    with_resolution: bool = True,
    **metadata_extra: Any,
) -> dict[str, Any]:
    """A minimal valid raw record mirroring sample_tickets_v6.json."""
    return {
        "conversation_id": conversation_id,
        "customer_id": "cust_abc",
        "messages": [
            {
                "message_id": f"{conversation_id}_msg001",
                "role": "customer",
                "text": "My pod is leaking.",
                "created_at": "2026-02-24T07:08:00Z",
            },
            {
                "message_id": f"{conversation_id}_msg002",
                "role": "agent",
                "text": "Sorry to hear that — let's take a look.",
                "created_at": "2026-02-24T07:12:00Z",
            },
        ],
        "metadata": {
            "category": "hardware",
            "issue_type": "leak",
            "product": "Pod 4",
            "status": status,
            "priority": "high",
            "created_at": "2026-02-24T07:00:00Z",
            "updated_at": "2026-02-24T08:00:00Z",
            "day": 1,
            **metadata_extra,
        },
        "resolution": (
            {
                "resolution_type": "replacement_unit",
                "resolution_notes": "Expedited replacement shipped.",
                "resolved_at": "2026-02-24T08:00:00Z",
            }
            if with_resolution
            else None
        ),
    }


def write_dataset(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "tickets.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_loader_parses_valid_records(tmp_path: Path) -> None:
    path = write_dataset(tmp_path, [make_record(), make_record("conv_0002", "open", False)])
    records = load_raw_conversations(path)
    assert len(records) == 2

    first = records[0]
    assert first.conversation_id == "conv_0001"
    assert first.customer_id == "cust_abc"
    assert [m.role for m in first.messages] == ["customer", "agent"]
    assert first.messages[0].text == "My pod is leaking."
    # Timestamps parse timezone-aware.
    assert first.messages[0].created_at == datetime(2026, 2, 24, 7, 8, tzinfo=UTC)
    assert first.metadata.created_at.tzinfo is not None
    assert first.resolution is not None
    assert first.resolution.resolution_type == "replacement_unit"
    # Unresolved conversations carry a null resolution.
    assert records[1].resolution is None


def test_loader_defaults_optional_metadata_flags(tmp_path: Path) -> None:
    plain = make_record()
    flagged = make_record(
        "conv_0002",
        has_curveball=True,
        is_multi_issue=True,
        secondary_issues=["temperature_control"],
    )
    records = load_raw_conversations(write_dataset(tmp_path, [plain, flagged]))
    assert records[0].metadata.has_curveball is False
    assert records[0].metadata.secondary_issues == []
    assert records[1].metadata.has_curveball is True
    assert records[1].metadata.secondary_issues == ["temperature_control"]


def test_loader_rejects_bad_role(tmp_path: Path) -> None:
    record = make_record()
    record["messages"][0]["role"] = "robot"
    with pytest.raises(ValueError, match="role"):
        load_raw_conversations(write_dataset(tmp_path, [record]))


def test_loader_rejects_missing_field(tmp_path: Path) -> None:
    record = make_record()
    del record["metadata"]["status"]
    with pytest.raises(ValueError, match="status"):
        load_raw_conversations(write_dataset(tmp_path, [record]))


def test_loader_rejects_empty_messages(tmp_path: Path) -> None:
    record = make_record()
    record["messages"] = []
    with pytest.raises(ValueError, match="messages"):
        load_raw_conversations(write_dataset(tmp_path, [record]))


def test_loader_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_raw_conversations(tmp_path / "nope.json")


# --- pure transforms --------------------------------------------------------


def test_conversation_row_maps_fields() -> None:
    from cxintel.ingestion.loader import RawConversation
    from cxintel.ingestion.service import conversation_row

    raw = RawConversation.model_validate(make_record(has_curveball=True))
    row = conversation_row(raw)

    assert row["external_id"] == "conv_0001"
    assert row["customer_id"] == "cust_abc"
    assert row["status"] == "resolved"
    assert row["priority"] == "high"
    assert row["category"] == "hardware"
    assert row["issue_type"] == "leak"
    assert row["product"] == "Pod 4"
    assert row["day"] == 1
    # started/ended derive from message activity, created/updated from metadata.
    assert row["started_at"] == datetime(2026, 2, 24, 7, 8, tzinfo=UTC)
    assert row["ended_at"] == datetime(2026, 2, 24, 7, 12, tzinfo=UTC)
    assert row["created_at"] == datetime(2026, 2, 24, 7, 0, tzinfo=UTC)
    assert row["updated_at"] == datetime(2026, 2, 24, 8, 0, tzinfo=UTC)
    assert row["resolution_type"] == "replacement_unit"
    assert row["resolved_at"] == datetime(2026, 2, 24, 8, 0, tzinfo=UTC)
    assert row["source_metadata"]["has_curveball"] is True


def test_conversation_row_ids_are_deterministic() -> None:
    from cxintel.ingestion.loader import RawConversation
    from cxintel.ingestion.service import conversation_row, message_rows

    raw = RawConversation.model_validate(make_record())
    row1, row2 = conversation_row(raw), conversation_row(raw)
    assert row1["id"] == row2["id"]

    msgs1 = message_rows(raw, row1["id"])
    msgs2 = message_rows(raw, row2["id"])
    assert [m["id"] for m in msgs1] == [m["id"] for m in msgs2]
    assert len({m["id"] for m in msgs1}) == len(msgs1)  # unique per message


def test_message_rows_map_fields() -> None:
    from cxintel.ingestion.loader import RawConversation
    from cxintel.ingestion.service import conversation_row, message_rows

    raw = RawConversation.model_validate(make_record())
    conv_id = conversation_row(raw)["id"]
    rows = message_rows(raw, conv_id)
    assert len(rows) == 2
    assert rows[0]["external_id"] == "conv_0001_msg001"
    assert rows[0]["conversation_id"] == conv_id
    assert rows[0]["role"] == "customer"
    assert rows[0]["body"] == "My pod is leaking."  # source field is `text`
    assert rows[0]["created_at"] == datetime(2026, 2, 24, 7, 8, tzinfo=UTC)


def test_unresolved_conversation_has_null_resolution_fields() -> None:
    from cxintel.ingestion.loader import RawConversation
    from cxintel.ingestion.service import conversation_row

    raw = RawConversation.model_validate(make_record(status="open", with_resolution=False))
    row = conversation_row(raw)
    assert row["resolution_type"] is None
    assert row["resolution_notes"] is None
    assert row["resolved_at"] is None


# --- IngestionService (integration — requires local Postgres) ---------------


def test_ingest_is_idempotent(tmp_path: Path, db_session: Any) -> None:
    from cxintel.ingestion.service import IngestionService
    from cxintel.repositories import ConversationRepository, MessageRepository

    path = write_dataset(
        tmp_path,
        [make_record(), make_record("conv_0002", "open", False)],
    )
    service = IngestionService(db_session)

    result = service.ingest(path)
    assert result.conversations_seen == 2
    assert result.conversations_inserted == 2
    assert result.messages_seen == 4
    assert result.messages_inserted == 4

    rerun = service.ingest(path)
    assert rerun.conversations_seen == 2
    assert rerun.conversations_inserted == 0
    assert rerun.messages_inserted == 0

    conv_repo = ConversationRepository(db_session)
    assert conv_repo.count() == 2
    assert MessageRepository(db_session).count() == 4

    # Spot-check derived fields survived the round trip.
    conv = conv_repo.get_by_external_id("conv_0001")
    assert conv is not None
    assert conv.status == "resolved"
    assert conv.resolution_type == "replacement_unit"
    assert conv.started_at == datetime(2026, 2, 24, 7, 8, tzinfo=UTC)
    assert conv.ended_at == datetime(2026, 2, 24, 7, 12, tzinfo=UTC)
    assert next(m.body for m in conv.messages) == "My pod is leaking."


def test_ingest_reports_live_progress(tmp_path: Path, db_session: Any) -> None:
    from cxintel.ingestion.service import IngestionService
    from cxintel.pipeline.progress import ProgressReporter

    path = write_dataset(
        tmp_path,
        [make_record(), make_record("conv_0002", "open", False)],
    )
    updates: list[Any] = []
    reporter = ProgressReporter(
        stage_key="ingest",
        stage_label="Data Ingestion",
        progress=updates.append,
        message="starting",
    )

    IngestionService(db_session).ingest(path, progress=reporter)

    final = [u for u in updates if hasattr(u, "completed_work")][-1]
    assert final.total_work == 2
    assert final.completed_work == 2
    assert final.percentage == 100
    assert final.current_item == "conv_0002"
