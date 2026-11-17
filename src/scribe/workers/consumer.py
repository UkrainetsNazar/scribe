import asyncio

import structlog

from scribe.application.event_handlers import (
    PipelineStartHandler,
    ProgressEventHandler,
    RecordingCleanupHandler,
    WebhookDeliveryHandler,
)
from scribe.application.inbox import EventHandler, InboxGuard
from scribe.container import Container
from scribe.infrastructure.messaging.consumer import RabbitMqConsumer
from scribe.infrastructure.messaging.topology import (
    PIPELINE_START,
    PROGRESS_EVENTS,
    RECORDING_CLEANUP,
    WEBHOOK_DELIVERY,
    QueueSpec,
)
from scribe.workers.common import connect_with_retry, init_observability, install_signal_handlers

log = structlog.get_logger()


async def main() -> None:
    container = Container()
    container.init_resources()
    settings = container.settings()
    init_observability(settings)

    # Queue -> handler
    handlers: list[tuple[QueueSpec, EventHandler]] = [
        (PIPELINE_START, PipelineStartHandler(container.pipeline_dispatcher())),
        (PROGRESS_EVENTS, ProgressEventHandler(container.progress_bus())),
        (
            RECORDING_CLEANUP,
            RecordingCleanupHandler(
                container.object_storage(),
                container.transcript_index(),
                [
                    settings.minio_bucket_uploads,
                    settings.minio_bucket_artifacts,
                    settings.minio_bucket_exports,
                ],
            ),
        ),
        (WEBHOOK_DELIVERY, WebhookDeliveryHandler(container.webhook_sender())),
    ]
    consumers = [
        RabbitMqConsumer(
            settings.rabbitmq_url,
            spec,
            InboxGuard(container.uow, spec.name, handler),
            settings.rabbitmq_retry_delays_sec,
            settings.rabbitmq_prefetch,
        )
        for spec, handler in handlers
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
        await container.redis().aclose()
        await container.mongo().close()
        container.shutdown_resources()


if __name__ == "__main__":
    asyncio.run(main())
