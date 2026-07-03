"""Typed live progress snapshots for executable pipeline stages."""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Lock

from pydantic import BaseModel, Field


class ProgressSnapshot(BaseModel):
    """Live progress for one executable stage.

    The snapshot is intentionally process-local UI state. Durable run history
    stays in ``pipeline_runs``; this model answers "what is happening right now?"
    for the control center.
    """

    stage_key: str
    stage_label: str
    total_work: int = Field(default=0, ge=0)
    completed_work: int = Field(default=0, ge=0)
    percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    estimated_remaining_seconds: float | None = Field(default=None, ge=0.0)
    throughput_conversations_per_second: float = Field(default=0.0, ge=0.0)
    current_item: str | None = None
    retry_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    message: str = ""

    def __str__(self) -> str:
        if self.message:
            return self.message
        if self.total_work:
            return f"{self.stage_label}: {self.completed_work}/{self.total_work}"
        return self.stage_label


ProgressUpdate = str | ProgressSnapshot
ProgressCallback = Callable[[ProgressUpdate], None]


def refresh_snapshot(snapshot: ProgressSnapshot, elapsed_seconds: float) -> ProgressSnapshot:
    """Return ``snapshot`` with elapsed-derived fields recalculated."""
    elapsed = max(0.0, elapsed_seconds)
    throughput = snapshot.completed_work / elapsed if elapsed > 0 else 0.0
    percentage = (
        min(100.0, (snapshot.completed_work / snapshot.total_work) * 100)
        if snapshot.total_work
        else 0.0
    )
    remaining: float | None
    if snapshot.total_work and snapshot.completed_work >= snapshot.total_work:
        remaining = 0.0
    elif snapshot.total_work and throughput > 0:
        remaining = (snapshot.total_work - snapshot.completed_work) / throughput
    else:
        remaining = None
    return snapshot.model_copy(
        update={
            "percentage": percentage,
            "elapsed_seconds": elapsed,
            "estimated_remaining_seconds": remaining,
            "throughput_conversations_per_second": throughput,
        }
    )


class ProgressReporter:
    """Small stage-local helper that owns progress math and callback emission."""

    def __init__(
        self,
        *,
        stage_key: str,
        stage_label: str,
        progress: ProgressCallback,
        total_work: int = 0,
        message: str = "",
    ) -> None:
        self._progress = progress
        self._started = time.monotonic()
        self._lock = Lock()
        self._snapshot = ProgressSnapshot(
            stage_key=stage_key,
            stage_label=stage_label,
            total_work=total_work,
            message=message,
        )
        self.report(message=message)

    @property
    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            return self._snapshot.model_copy()

    def report(
        self,
        *,
        message: str | None = None,
        total_work: int | None = None,
        completed_work: int | None = None,
        current_item: str | None = None,
    ) -> None:
        """Emit the current snapshot, optionally updating selected fields first."""
        with self._lock:
            updates: dict[str, object] = {}
            if message is not None:
                updates["message"] = message
            if total_work is not None:
                updates["total_work"] = max(0, total_work)
            if completed_work is not None:
                updates["completed_work"] = max(0, completed_work)
            if current_item is not None:
                updates["current_item"] = current_item
            snapshot = self._snapshot.model_copy(update=updates) if updates else self._snapshot
            self._snapshot = refresh_snapshot(snapshot, time.monotonic() - self._started)
            emitted = self._snapshot.model_copy()
        self._progress(emitted)

    def set_current(self, current_item: str, *, message: str | None = None) -> None:
        self.report(current_item=current_item, message=message)

    def advance(
        self,
        *,
        current_item: str | None = None,
        count: int = 1,
        failed: bool = False,
        message: str | None = None,
    ) -> None:
        with self._lock:
            completed = self._snapshot.completed_work + max(0, count)
            failures = self._snapshot.failure_count + (1 if failed else 0)
            updates: dict[str, object] = {
                "completed_work": completed,
                "failure_count": failures,
            }
            if current_item is not None:
                updates["current_item"] = current_item
            if message is not None:
                updates["message"] = message
            snapshot = self._snapshot.model_copy(update=updates)
            self._snapshot = refresh_snapshot(snapshot, time.monotonic() - self._started)
            emitted = self._snapshot.model_copy()
        self._progress(emitted)

    def retry(self, *, current_item: str | None = None, message: str | None = None) -> None:
        with self._lock:
            updates: dict[str, object] = {"retry_count": self._snapshot.retry_count + 1}
            if current_item is not None:
                updates["current_item"] = current_item
            if message is not None:
                updates["message"] = message
            snapshot = self._snapshot.model_copy(update=updates)
            self._snapshot = refresh_snapshot(snapshot, time.monotonic() - self._started)
            emitted = self._snapshot.model_copy()
        self._progress(emitted)

    def mark_failed(self, *, message: str) -> None:
        with self._lock:
            snapshot = self._snapshot.model_copy(
                update={
                    "failure_count": max(1, self._snapshot.failure_count),
                    "message": message,
                }
            )
            self._snapshot = refresh_snapshot(snapshot, time.monotonic() - self._started)
            emitted = self._snapshot.model_copy()
        self._progress(emitted)
