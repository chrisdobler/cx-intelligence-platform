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

from .progress import ProgressCallback, ProgressSnapshot, ProgressUpdate, refresh_snapshot


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
    progress_detail: ProgressSnapshot | None = None
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
            if self._job is None:
                return None
            return self._refresh_job(self._job).model_copy(deep=True)

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
                    detail = self._job.progress_detail
                    if detail is None:
                        detail = ProgressSnapshot(
                            stage_key=self._job.target,
                            stage_label=self._job.target,
                            failure_count=1,
                            message=str(exc),
                        )
                    else:
                        detail = detail.model_copy(
                            update={
                                "failure_count": max(1, detail.failure_count),
                                "message": str(exc),
                            }
                        )
                    self._job.progress_detail = self._refresh_snapshot(detail, self._job)
                    self._job.progress = str(self._job.progress_detail)
                    self._job.state = JobState.FAILED
                    self._job.error = str(exc)
                    self._job.finished_at = datetime.now(tz=UTC)
                return
            with self._lock:
                assert self._job is not None
                self._job.state = JobState.SUCCEEDED
                self._job.message = message
                self._job.progress = message
                if self._job.progress_detail is not None:
                    self._job.progress_detail = self._refresh_snapshot(
                        self._job.progress_detail.model_copy(update={"message": message}),
                        self._job,
                    )
                self._job.finished_at = datetime.now(tz=UTC)

        self._spawn(worker)
        return snapshot

    def _set_progress(self, update: ProgressUpdate) -> None:
        with self._lock:
            if self._job is not None:
                if isinstance(update, ProgressSnapshot):
                    self._job.progress_detail = self._refresh_snapshot(update, self._job)
                    self._job.progress = str(self._job.progress_detail)
                else:
                    self._job.progress = update

    def _refresh_job(self, job: Job) -> Job:
        snapshot = job.model_copy(deep=True)
        if snapshot.progress_detail is not None:
            snapshot.progress_detail = self._refresh_snapshot(snapshot.progress_detail, snapshot)
        return snapshot

    def _refresh_snapshot(self, snapshot: ProgressSnapshot, job: Job) -> ProgressSnapshot:
        if job.started_at is None:
            return snapshot
        finished_at = job.finished_at or datetime.now(tz=UTC)
        elapsed = (finished_at - job.started_at).total_seconds()
        return refresh_snapshot(snapshot, elapsed)

    def _spawn(self, worker: Callable[[], None]) -> None:
        # Seam for tests, which replace this with an inline call.
        threading.Thread(target=worker, daemon=True).start()


TRACKER = JobTracker()
