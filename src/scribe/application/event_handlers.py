from uuid import UUID

import structlog

from scribe.application.inbox import EventHandler, InboundEvent, PermanentHandlerError
from scribe.application.interfaces import UnitOfWork
from scribe.application.pipeline_ports import PipelineDispatcher

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
