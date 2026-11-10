import structlog

from scribe.application.inbox import EventHandler, InboundEvent
from scribe.application.interfaces import UnitOfWork

log = structlog.get_logger()


class PipelineStartHandler(EventHandler):
    # Reacts to `recording.processing_requested`.

    async def handle(self, event: InboundEvent, uow: UnitOfWork) -> None:
        log.info(
            "pipeline.start_requested",
            recording_id=event.payload.get("recording_id"),
            event_id=str(event.event_id),
        )
