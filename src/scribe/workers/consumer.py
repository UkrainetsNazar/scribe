import asyncio

import structlog

from scribe.application.event_handlers import PipelineStartHandler
from scribe.application.inbox import EventHandler, InboxGuard
from scribe.container import Container
from scribe.infrastructure.messaging.consumer import RabbitMqConsumer
from scribe.infrastructure.messaging.topology import PIPELINE_START, QueueSpec
from scribe.workers.common import connect_with_retry, install_signal_handlers

log = structlog.get_logger()

HANDLERS: list[tuple[QueueSpec, EventHandler]] = [(PIPELINE_START, PipelineStartHandler())]


async def main() -> None:
    container = Container()
    container.init_resources()
    settings = container.settings()

    consumers = [
        RabbitMqConsumer(
            settings.rabbitmq_url,
            spec,
            InboxGuard(
                container.uow, spec.name, handler
            ),  # container.uow creates a new UoW per call
            settings.rabbitmq_retry_delays_sec,
            settings.rabbitmq_prefetch,
        )
        for spec, handler in HANDLERS
    ]
    for consumer in consumers:
        await connect_with_retry(consumer.start, f"consumer:{consumer.name}")

    stop = asyncio.Event()
    install_signal_handlers(stop)
    log.info("consumer.ready", queues=[c.name for c in consumers])
    try:
        await stop.wait()
    finally:
        log.info("consumer.stopping")
        for consumer in consumers:
            await consumer.close()
        await container.engine().dispose()
        container.shutdown_resources()


if __name__ == "__main__":
    asyncio.run(main())
