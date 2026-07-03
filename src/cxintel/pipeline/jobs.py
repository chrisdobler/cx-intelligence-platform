"""Background execution of pipeline jobs for the API and control center.

A single in-process worker thread runs one job at a time — intentionally the
simplest machinery that supports "click Run, watch progress" on the landing
page (no queue, no broker; see the complexity budget in ARCHITECTURE.md).
Job state is in memory: it resets on restart, and the finished job stays
visible until the next one starts.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel

from .stages import ProgressCallback


class JobState(StrEnum):
    """Lifecycle of a background pipeline job."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(BaseModel):
    """Observable state of one background job (a stage key or 'pipeline')."""

    target: str
    state: JobState
    progress: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None
    error: str | None = None


class JobBusyError(Exception):
    """Raised when a job is started while another is still running."""


class JobTracker:
    """Runs one job at a time on a daemon thread and tracks its state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: Job | None = None

    def current(self) -> Job | None:
        """The running job, or the most recently finished one."""
        with self._lock:
            return self._job.model_copy() if self._job else None

    def start(self, target: str, fn: Callable[[ProgressCallback], str]) -> Job:
        """Start ``fn`` in the background; raises ``JobBusyError`` if busy."""
        with self._lock:
            if self._job is not None and self._job.state is JobState.RUNNING:
                raise JobBusyError(f"'{self._job.target}' is still running.")
            self._job = Job(
                target=target,
                state=JobState.RUNNING,
                progress="Starting…",
                started_at=datetime.now(tz=UTC),
            )
            snapshot = self._job.model_copy()

        def worker() -> None:
            try:
                message = fn(self._set_progress)
            except Exception as exc:
                with self._lock:
                    assert self._job is not None
                    self._job.state = JobState.FAILED
                    self._job.error = str(exc)
                    self._job.finished_at = datetime.now(tz=UTC)
                return
            with self._lock:
                assert self._job is not None
                self._job.state = JobState.SUCCEEDED
                self._job.message = message
                self._job.progress = message
                self._job.finished_at = datetime.now(tz=UTC)

        self._spawn(worker)
        return snapshot

    def _set_progress(self, message: str) -> None:
        with self._lock:
            if self._job is not None:
                self._job.progress = message

    def _spawn(self, worker: Callable[[], None]) -> None:
        # Seam for tests, which replace this with an inline call.
        threading.Thread(target=worker, daemon=True).start()


TRACKER = JobTracker()
