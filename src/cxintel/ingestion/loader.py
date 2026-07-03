"""Raw dataset loading and validation.

Pydantic models mirroring the structure of ``sample_tickets_v6.json``. Every
record is validated on load so malformed input fails fast with a clear error
instead of producing partial rows downstream. Optional metadata flags are
omitted from the source when false, so they default here rather than being
required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, TypeAdapter


class RawMessage(BaseModel):
    """One message turn as it appears in the source dataset."""

    message_id: str
    role: Literal["customer", "agent"]
    text: str
    created_at: AwareDatetime


class RawResolution(BaseModel):
    """Resolution details, present only for resolved conversations."""

    resolution_type: str
    resolution_notes: str
    resolved_at: AwareDatetime


class RawMetadata(BaseModel):
    """Per-conversation source metadata. Flag fields are absent when false."""

    category: str
    issue_type: str
    product: str
    status: str
    priority: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    day: int
    has_curveball: bool = False
    spans_multiple_days: bool = False
    is_long_conversation: bool = False
    is_multi_issue: bool = False
    secondary_issues: list[str] = Field(default_factory=list)


class RawConversation(BaseModel):
    """One complete ticket record from the source dataset."""

    conversation_id: str
    customer_id: str
    messages: list[RawMessage] = Field(min_length=1)
    metadata: RawMetadata
    resolution: RawResolution | None


_DATASET_ADAPTER: TypeAdapter[list[RawConversation]] = TypeAdapter(list[RawConversation])


def load_raw_conversations(path: Path) -> list[RawConversation]:
    """Load and validate the raw ticket dataset from ``path``.

    Raises ``FileNotFoundError`` if the file is missing and
    ``pydantic.ValidationError`` (a ``ValueError``) if any record is malformed.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    return _DATASET_ADAPTER.validate_python(data)
