from collections.abc import Sequence
from uuid import UUID

import structlog

from scribe.application.inbox import EventHandler, InboundEvent, PermanentHandlerError
from scribe.application.interfaces import ObjectStorage, UnitOfWork
from scribe.application.pipeline_ports import PipelineDispatcher, TranscriptIndex
from scribe.application.progress import (
    Message,
    ProgressPublisher,
    cancelled_message,
    completed_message,
    failed_message,
    status_message,
)
from scribe.application.webhooks import WebhookDelivery, WebhookSender, build_webhook_data
from scribe.domain.webhooks import webhook_body

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


class RecordingCleanupHandler(EventHandler):
    """Reacts to `recording.deleted` (FR-14): removes files, transcript documents and, last,
    the database rows. Every step is idempotent, so a retry after a failure is safe."""

    def __init__(
        self, storage: ObjectStorage, index: TranscriptIndex, buckets: Sequence[str]
    ) -> None:
        self._storage = storage
        self._index = index
        self._buckets = tuple(buckets)

    async def handle(self, event: InboundEvent, uow: UnitOfWork) -> None:
        try:
            recording_id = UUID(str(event.payload["recording_id"]))
            owner_id = UUID(str(event.payload["owner_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise PermanentHandlerError(f"Malformed recording.deleted payload: {exc!r}") from exc
        prefix = f"{owner_id}/{recording_id}/"  # sources, artifacts and exports share it
        files = 0
        for bucket in self._buckets:
            files += await self._storage.delete_prefix(bucket, prefix)
        await self._index.remove(recording_id)
        row_removed = await uow.recordings.purge(recording_id)
        log.info(
            "recording.purged", recording_id=str(recording_id), files=files, row_removed=row_removed
        )


class WebhookDeliveryHandler(EventHandler):
    """Reacts to `recording.completed` and `recording.failed` by calling the project's webhook.

    Delivery is at-least-once: if the inbox commit fails after a successful call, the event is
    redelivered and the webhook is sent again with the same X-Scribe-Delivery id."""

    def __init__(self, sender: WebhookSender) -> None:
        self._sender = sender

    async def handle(self, event: InboundEvent, uow: UnitOfWork) -> None:
        try:
            recording_id = UUID(str(event.payload["recording_id"]))
            owner_id = UUID(str(event.payload["owner_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise PermanentHandlerError(f"Malformed {event.event_type} payload: {exc!r}") from exc

        recording = await uow.recordings.get(recording_id, owner_id)
        if recording is None:  # deleted in the meantime
            return
        project = await uow.projects.get(recording.project_id, owner_id)
        if project is None or not project.webhook_url or not project.webhook_secret:
            return  # no webhook configured

        body = webhook_body(
            event.event_id,
            event.event_type,
            str(event.payload.get("occurred_at", "")),
            build_webhook_data(event.event_type, event.payload, recording),
        )
        await self._sender.send(
            WebhookDelivery(
                url=project.webhook_url,
                secret=project.webhook_secret,
                event_id=event.event_id,
                event_type=event.event_type,
                body=body,
            )
        )
        # The URL is not logged: it may contain a token
        log.info(
            "webhook.delivered",
            recording_id=str(recording_id),
            event_type=event.event_type,
            event_id=str(event.event_id),
        )
