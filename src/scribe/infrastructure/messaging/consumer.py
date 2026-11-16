import contextlib
import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import aio_pika
import orjson
import structlog
from aio_pika.abc import (
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractRobustConnection,
)
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from scribe.application.inbox import InboundEvent, InboxGuard, PermanentHandlerError
from scribe.infrastructure.messaging.topology import (
    QueueSpec,
    Topology,
    declare_topology,
    retry_routing_key,
)
from scribe.infrastructure.metrics import CONSUMER_DURATION, CONSUMER_MESSAGES, INBOX_DUPLICATES

log = structlog.get_logger()
_tracer = trace.get_tracer("scribe.consumer")

RETRY_COUNT_HEADER = "x-retry-count"


def parse_event(message: AbstractIncomingMessage) -> InboundEvent:
    """Raises ValueError for a malformed message (it can never succeed, so it is not retried)."""
    payload = orjson.loads(message.body)
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    return InboundEvent(
        event_id=UUID(message.message_id or ""),
        event_type=message.type or "",
        payload=payload,
        headers=dict(message.headers),
    )


def _retry_count(message: AbstractIncomingMessage) -> int:
    value = message.headers.get(RETRY_COUNT_HEADER, 0)
    return value if isinstance(value, int) else 0


def _clean_headers(message: AbstractIncomingMessage) -> dict[str, Any]:
    # Drop the broker-managed dead-letter bookkeeping before republishing
    return {
        k: v
        for k, v in message.headers.items()
        if k != "x-death" and not k.startswith(("x-first-death", "x-last-death"))
    }


class RabbitMqConsumer:
    """Consumes one queue with manual acks, idempotent processing, retries and a DLQ."""

    def __init__(
        self,
        url: str,
        queue: QueueSpec,
        guard: InboxGuard,
        retry_delays: Sequence[int],
        prefetch: int = 10,
    ) -> None:
        self._url = url
        self._queue = queue
        self._guard = guard
        self._retry_delays = tuple(retry_delays)
        self._prefetch = prefetch
        self._connection: AbstractRobustConnection | None = None
        self._topology: Topology | None = None

    @property
    def name(self) -> str:
        return self._queue.name

    async def start(self) -> None:
        try:
            self._connection = await aio_pika.connect_robust(self._url)
            channel = await self._connection.channel()
            await channel.set_qos(prefetch_count=self._prefetch)
            self._topology = await declare_topology(channel, self._retry_delays)
            await self._topology.queues[self._queue.name].consume(self._on_message)
        except Exception:
            await self.close()
            raise
        log.info("consumer.started", queue=self._queue.name)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        try:
            await self._handle(message)
        except Exception:
            # Could not ack or republish: put the message back, the inbox absorbs duplicates
            log.exception("consumer.settle_failed", queue=self._queue.name)
            with contextlib.suppress(Exception):
                await message.nack(requeue=True)

    async def _handle(self, message: AbstractIncomingMessage) -> None:
        queue = self._queue.name
        try:
            event = parse_event(message)
        except ValueError as exc:
            log.warning("consumer.malformed_message", queue=queue, error=str(exc))
            await self._dead_letter(message, f"malformed message: {exc}")
            await message.ack()
            return

        recording_id = event.payload.get("recording_id")
        if recording_id:
            structlog.contextvars.bind_contextvars(recording_id=str(recording_id))
        started = time.perf_counter()
        with _tracer.start_as_current_span(
            f"{event.event_type} process",
            context=propagate.extract(event.headers),
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "rabbitmq",
                "messaging.destination.name": queue,
                "messaging.message.id": str(event.event_id),
            },
        ) as span:
            try:
                processed = await self._guard.process(event)
            except PermanentHandlerError as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                log.error("consumer.permanent_error", event_id=str(event.event_id), error=str(exc))
                await self._dead_letter(message, f"permanent error: {exc}")
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, repr(exc)[:200]))
                log.exception("consumer.handler_failed", event_id=str(event.event_id))
                await self._retry_or_dead_letter(message, exc)
            else:
                if processed:
                    CONSUMER_MESSAGES.labels(queue, "processed").inc()
                else:
                    CONSUMER_MESSAGES.labels(queue, "duplicate").inc()
                    INBOX_DUPLICATES.labels(queue).inc()
                    log.info("consumer.duplicate_skipped", event_id=str(event.event_id))
        CONSUMER_DURATION.labels(queue).observe(time.perf_counter() - started)
        # Ack only after the message is processed or safely republished
        await message.ack()

    async def _retry_or_dead_letter(
        self, message: AbstractIncomingMessage, error: Exception
    ) -> None:
        topology = self._require_topology()
        attempt = _retry_count(message)
        if attempt >= len(self._retry_delays):
            await self._dead_letter(message, f"retries exhausted: {error!r}")
            return
        await self._republish(
            topology.retry,
            retry_routing_key(self._queue.name, attempt),
            message,
            {RETRY_COUNT_HEADER: attempt + 1, "x-last-error": repr(error)[:500]},
        )
        CONSUMER_MESSAGES.labels(self._queue.name, "retried").inc()
        log.info("consumer.scheduled_retry", queue=self._queue.name, attempt=attempt + 1)

    async def _dead_letter(self, message: AbstractIncomingMessage, reason: str) -> None:
        await self._republish(
            self._require_topology().dlx,
            self._queue.name,
            message,
            {"x-dead-letter-reason": reason[:500]},
        )
        CONSUMER_MESSAGES.labels(self._queue.name, "dead_lettered").inc()
        log.error("consumer.dead_lettered", queue=self._queue.name, reason=reason[:200])

    async def _republish(
        self,
        exchange: AbstractExchange,
        routing_key: str,
        message: AbstractIncomingMessage,
        extra_headers: dict[str, Any],
    ) -> None:
        headers = _clean_headers(message)
        headers.update(extra_headers)
        await exchange.publish(
            aio_pika.Message(
                body=message.body,
                content_type=message.content_type,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=message.message_id,
                type=message.type,
                timestamp=message.timestamp,
                headers=headers,
            ),
            routing_key=routing_key,
            mandatory=False,
        )

    def _require_topology(self) -> Topology:
        if self._topology is None:
            raise RuntimeError("Consumer is not started")
        return self._topology
