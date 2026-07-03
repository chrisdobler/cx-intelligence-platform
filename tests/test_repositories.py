"""Integration tests for the repository layer (require local Postgres)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from cxintel.repositories import ConversationRepository, MessageRepository


def conversation_row(external_id: str, status: str = "open", day: int = 1) -> dict[str, Any]:
    ts = datetime(2026, 2, 24, 12, 0, tzinfo=UTC)
    return {
        "id": uuid.uuid5(uuid.NAMESPACE_URL, external_id),
        "external_id": external_id,
        "customer_id": "cust_x",
        "status": status,
        "priority": "medium",
        "category": "hardware",
        "issue_type": "leak",
        "product": "Pod 4",
        "day": day,
        "started_at": ts,
        "ended_at": ts,
        "created_at": ts,
        "updated_at": ts,
        "resolution_type": None,
        "resolution_notes": None,
        "resolved_at": None,
        "source_metadata": None,
    }


def test_bulk_insert_is_idempotent(db_session: Session) -> None:
    repo = ConversationRepository(db_session)
    rows = [conversation_row("conv_a"), conversation_row("conv_b")]
    assert repo.bulk_insert_ignore_conflicts(rows) == 2
    db_session.commit()
    # Rerun: same rows conflict on external_id/pk and are skipped.
    assert repo.bulk_insert_ignore_conflicts(rows) == 0
    db_session.commit()
    assert repo.count() == 2


def test_count_by_status_groups(db_session: Session) -> None:
    repo = ConversationRepository(db_session)
    repo.bulk_insert_ignore_conflicts(
        [
            conversation_row("conv_a", status="resolved"),
            conversation_row("conv_b", status="resolved"),
            conversation_row("conv_c", status="escalated"),
        ]
    )
    db_session.commit()
    assert repo.count_by_status() == {"resolved": 2, "escalated": 1}


def test_date_range(db_session: Session) -> None:
    repo = ConversationRepository(db_session)
    assert repo.date_range() is None  # empty table

    early = conversation_row("conv_a")
    early["started_at"] = datetime(2026, 2, 24, 0, 0, tzinfo=UTC)
    late = conversation_row("conv_b")
    late["ended_at"] = datetime(2026, 3, 5, 19, 24, tzinfo=UTC)
    repo.bulk_insert_ignore_conflicts([early, late])
    db_session.commit()

    date_range = repo.date_range()
    assert date_range is not None
    assert date_range[0] == datetime(2026, 2, 24, 0, 0, tzinfo=UTC)
    assert date_range[1] == datetime(2026, 3, 5, 19, 24, tzinfo=UTC)


def test_get_by_external_id(db_session: Session) -> None:
    repo = ConversationRepository(db_session)
    repo.bulk_insert_ignore_conflicts([conversation_row("conv_a")])
    db_session.commit()
    found = repo.get_by_external_id("conv_a")
    assert found is not None
    assert found.external_id == "conv_a"
    assert repo.get_by_external_id("conv_missing") is None


def test_message_repository_insert_and_count(db_session: Session) -> None:
    conv_repo = ConversationRepository(db_session)
    conv_repo.bulk_insert_ignore_conflicts([conversation_row("conv_a")])
    msg_repo = MessageRepository(db_session)
    conv_id = uuid.uuid5(uuid.NAMESPACE_URL, "conv_a")
    rows = [
        {
            "id": uuid.uuid5(uuid.NAMESPACE_URL, "conv_a_msg001"),
            "external_id": "conv_a_msg001",
            "conversation_id": conv_id,
            "role": "customer",
            "body": "hello",
            "created_at": datetime(2026, 2, 24, 12, 0, tzinfo=UTC),
        }
    ]
    assert msg_repo.bulk_insert_ignore_conflicts(rows) == 1
    assert msg_repo.bulk_insert_ignore_conflicts(rows) == 0
    db_session.commit()
    assert msg_repo.count() == 1
