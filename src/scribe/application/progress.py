import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Final
from uuid import UUID

import structlog

from scribe.domain.pipeline import ProcessingStage, StageName, StageStatus, overall_percent
from scribe.domain.recordings import Recording, RecordingStatus
from scribe.domain.transcript import Segment

log = structlog.get_logger()

Message = dict[str, Any]

TERMINAL_TYPES: Final = frozenset({"completed", "failed", "cancelled"})


# ── Message builders (the WebSocket wire format) ─────────────────────────────


def status_message(recording_id: UUID, run: int, status: str) -> Message:
    return {"type": "status", "recording_id": str(recording_id), "run": run, "status": status}


def progress_message(
    recording_id: UUID, run: int, stage: StageName, percent: int, attempt: int = 1
) -> Message:
    percent = max(0, min(100, percent))
    return {
        "type": "progress",
        "recording_id": str(recording_id),
        "run": run,
        "stage": stage.value,
        "percent": percent,
        "overall": overall_percent(stage, percent),
        "attempt": attempt,
    }


def segment_message(recording_id: UUID, run: int, segment: Segment) -> Message:
    return {
        "type": "segment",
        "recording_id": str(recording_id),
        "run": run,
        "start": segment.start,
        "end": segment.end,
        "text": segment.text,
    }


def completed_message(recording_id: UUID, run: int) -> Message:
    return {"type": "completed", "recording_id": str(recording_id), "run": run}


def failed_message(recording_id: UUID, run: int, stage: str | None, error: str) -> Message:
    return {
        "type": "failed",
        "recording_id": str(recording_id),
        "run": run,
        "stage": stage,
        "error": error,
    }


def cancelled_message(recording_id: UUID, run: int) -> Message:
    return {"type": "cancelled", "recording_id": str(recording_id), "run": run}


# ── Ports ────────────────────────────────────────────────────────────────────


class ProgressPublisher(ABC):
    @abstractmethod
    async def publish_state(self, recording_id: UUID, message: Message) -> None:
        """Stores the message as the latest state and broadcasts it.

        A message is dropped when a newer state is already stored (a higher run, or the same
        run with a higher rank: status < progress < completed/failed/cancelled).
        """

    @abstractmethod
    async def publish_transient(self, recording_id: UUID, message: Message) -> None:
        """Broadcasts without storing (partial segments)."""


class ProgressSubscription(ABC):
    @abstractmethod
    async def last_state(self) -> Message | None: ...

    @abstractmethod
    async def next_message(self, timeout: float) -> Message | None:  # noqa: ASYNC109
        """Waits up to `timeout` seconds; None on timeout."""


class ProgressSubscriber(ABC):
    @abstractmethod
    def subscribe(self, recording_id: UUID) -> AbstractAsyncContextManager[ProgressSubscription]:
        """The subscription is active before this returns, so reading `last_state()` afterwards
        cannot miss a message: at worst a message is seen twice (state and stream)."""


class NullProgressPublisher(ProgressPublisher):
    async def publish_state(self, recording_id: UUID, message: Message) -> None:
        return None

    async def publish_transient(self, recording_id: UUID, message: Message) -> None:
        return None


class BestEffortPublisher(ProgressPublisher):
    """Progress is ephemeral: a slow or broken Redis must never fail or stall a pipeline stage."""

    def __init__(self, inner: ProgressPublisher, timeout_sec: float = 2.0) -> None:
        self._inner = inner
        self._timeout = timeout_sec

    async def publish_state(self, recording_id: UUID, message: Message) -> None:
        await self._quietly(recording_id, self._inner.publish_state(recording_id, message))

    async def publish_transient(self, recording_id: UUID, message: Message) -> None:
        await self._quietly(recording_id, self._inner.publish_transient(recording_id, message))

    async def _quietly(self, recording_id: UUID, call: Awaitable[None]) -> None:
        try:
            await asyncio.wait_for(call, timeout=self._timeout)
        except Exception:
            log.warning("progress.publish_failed", recording_id=str(recording_id), exc_info=True)


# ── Snapshots (reconnect and fallback when Redis has no state) ───────────────


def snapshot_message(recording: Recording, stages: Sequence[ProcessingStage]) -> Message:
    rid, run, status = recording.id, recording.run, recording.status
    if status is RecordingStatus.COMPLETED:
        return completed_message(rid, run)
    if status is RecordingStatus.CANCELLED:
        return cancelled_message(rid, run)
    if status is RecordingStatus.FAILED:
        failed = next((s for s in stages if s.status is StageStatus.FAILED), None)
        if failed is None:
            return failed_message(rid, run, None, "Processing failed")
        return failed_message(rid, run, failed.name.value, failed.error or "Processing failed")
    if status is RecordingStatus.PROCESSING:
        current = next((s for s in stages if s.status is not StageStatus.COMPLETED), None)
        if current is not None:
            return progress_message(
                rid, run, current.name, current.progress, max(current.attempts, 1)
            )
    return status_message(rid, run, status.value)


def initial_message(
    stored: Message | None, recording: Recording, stages: Sequence[ProcessingStage]
) -> Message:
    """The first message of a connection: the stored state, unless it is from an older run."""
    if stored is not None:
        run = stored.get("run")
        if isinstance(run, int) and run >= recording.run:
            return stored
    return snapshot_message(recording, stages)
