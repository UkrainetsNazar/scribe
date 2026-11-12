from uuid import UUID

import structlog

from scribe.application.inbox import EventHandler, InboundEvent, PermanentHandlerError
from scribe.application.interfaces import UnitOfWork
from scribe.application.pipeline_ports import PipelineDispatcher
from scribe.application.progress import (
    Message,
    ProgressPublisher,
    cancelled_message,
    completed_message,
    failed_message,
    status_message,
)

log = structlog.get_logger()


class PipelineStartHandler(EventHandler):
    """Reacts to `recording.processing_requested` by queueing the first pipeline stage.

    The dispatch happens before the inbox transaction commits. If the commit fails, the event
    is redelivered and dispatched again; the pipeline stages are idempotent.
    """

    def __init__(self, dispatcher: PipelineDispatcher) -> None:
        self._dispatcher = dispatcher

    async def handle(self, event: InboundEvent, uow: UnitOfWork) -> None:
        try:
            recording_id = UUID(str(event.payload["recording_id"]))
            owner_id = UUID(str(event.payload["owner_id"]))
            run = int(event.payload["run"])
        except (KeyError, ValueError, TypeError) as exc:
            raise PermanentHandlerError(f"Malformed processing_requested payload: {exc!r}") from exc
        await self._dispatcher.dispatch(recording_id, owner_id, run)
        log.info("pipeline.dispatched", recording_id=str(recording_id), run=run)


class ProgressEventHandler(EventHandler):
    """Turns recording status events into WebSocket messages (Redis state + Pub/Sub).

    Unlike the pipeline's best-effort progress, errors here are raised, so the consumer
    retries and the message eventually reaches the DLQ instead of being silently lost.
    """

    def __init__(self, progress: ProgressPublisher) -> None:
        self._progress = progress

    async def handle(self, event: InboundEvent, uow: UnitOfWork) -> None:
        try:
            recording_id = UUID(str(event.payload["recording_id"]))
            run = int(event.payload.get("run", 0))
        except (KeyError, ValueError, TypeError) as exc:
            raise PermanentHandlerError(f"Malformed {event.event_type} payload: {exc!r}") from exc
        message = self._message(event, recording_id, run)
        if message is not None:
            await self._progress.publish_state(recording_id, message)

    @staticmethod
    def _message(event: InboundEvent, recording_id: UUID, run: int) -> Message | None:
        match event.event_type:
            case "recording.processing_requested":
                return status_message(recording_id, run, "processing")
            case "recording.completed":
                return completed_message(recording_id, run)
            case "recording.failed":
                reason = str(event.payload.get("reason") or "Processing failed")
                return failed_message(recording_id, run, event.payload.get("stage"), reason)
            case "recording.cancelled":
                return cancelled_message(recording_id, run)
            case _:
                return None
