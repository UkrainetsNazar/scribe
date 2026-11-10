import asyncio
from collections.abc import Sequence

import aio_pika
import orjson
from aio_pika.abc import AbstractRobustConnection

from scribe.application.messaging import MessagePublisher, OutboxMessage
from scribe.infrastructure.messaging.topology import Topology, declare_topology


class RabbitMqPublisher(MessagePublisher):
    def __init__(self, url: str, retry_delays: Sequence[int], timeout_sec: float = 10.0) -> None:
        self._url = url
        self._retry_delays = tuple(retry_delays)
        self._timeout = timeout_sec
        self._connection: AbstractRobustConnection | None = None
        self._topology: Topology | None = None

    async def connect(self) -> None:
        # connect_robust reconnects automatically after the first successful connection
        self._connection = await aio_pika.connect_robust(self._url)
        channel = await self._connection.channel()  # publisher confirms are on by default
        self._topology = await declare_topology(channel, self._retry_delays)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def publish(self, message: OutboxMessage) -> None:
        if self._topology is None:
            raise RuntimeError("Publisher is not connected")
        amqp_message = aio_pika.Message(
            body=orjson.dumps(message.payload),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=str(message.event_id),
            type=message.event_type,
            timestamp=message.created_at,
            headers={k: v for k, v in message.headers.items() if v is not None},
        )
        # Events nobody subscribes to are simply dropped by the broker, hence mandatory=False
        await asyncio.wait_for(
            self._topology.events.publish(
                amqp_message, routing_key=message.event_type, mandatory=False
            ),
            timeout=self._timeout,
        )
