from collections.abc import Sequence
from dataclasses import dataclass

from aio_pika import ExchangeType
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue

EVENTS_EXCHANGE = "scribe.events"
RETRY_EXCHANGE = "scribe.retry"
DLX_EXCHANGE = "scribe.dlx"


@dataclass(frozen=True, slots=True)
class QueueSpec:
    name: str
    routing_keys: tuple[str, ...]


PIPELINE_START = QueueSpec("pipeline.start", ("recording.processing_requested",))
QUEUES = (PIPELINE_START,)


@dataclass(frozen=True, slots=True)
class Topology:
    events: AbstractExchange
    retry: AbstractExchange
    dlx: AbstractExchange
    queues: dict[str, AbstractQueue]


def retry_queue_name(queue: str, tier: int, delay_sec: int) -> str:
    return f"{queue}.retry.{tier}.{delay_sec}s"


def retry_routing_key(queue: str, tier: int) -> str:
    return f"{queue}.{tier}"


async def declare_topology(channel: AbstractChannel, retry_delays: Sequence[int]) -> Topology:
    """Idempotent. Both the relay and the consumers call it on startup.

    An event published before its queue exists is dropped by the broker (unroutable),
    so whoever publishes must declare the queues first.
    """
    events = await channel.declare_exchange(EVENTS_EXCHANGE, ExchangeType.TOPIC, durable=True)
    retry = await channel.declare_exchange(RETRY_EXCHANGE, ExchangeType.DIRECT, durable=True)
    dlx = await channel.declare_exchange(DLX_EXCHANGE, ExchangeType.DIRECT, durable=True)

    queues: dict[str, AbstractQueue] = {}
    for spec in QUEUES:
        main = await channel.declare_queue(spec.name, durable=True)
        for key in spec.routing_keys:
            await main.bind(events, routing_key=key)

        dead = await channel.declare_queue(f"{spec.name}.dlq", durable=True)
        await dead.bind(dlx, routing_key=spec.name)

        for tier, delay in enumerate(retry_delays):
            waiting = await channel.declare_queue(
                retry_queue_name(spec.name, tier, delay),
                durable=True,
                arguments={
                    "x-message-ttl": delay * 1000,
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": spec.name,
                },
            )
            await waiting.bind(retry, routing_key=retry_routing_key(spec.name, tier))
        queues[spec.name] = main
    return Topology(events=events, retry=retry, dlx=dlx, queues=queues)
