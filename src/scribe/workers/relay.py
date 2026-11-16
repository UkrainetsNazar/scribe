import asyncio

import structlog

from scribe.container import Container
from scribe.workers.common import connect_with_retry, init_observability, install_signal_handlers

log = structlog.get_logger()


async def main() -> None:
    container = Container()
    container.init_resources()  # configures logging
    init_observability(container.settings())
    publisher = container.message_publisher()
    relay = container.outbox_relay()

    await connect_with_retry(publisher.connect, "relay")
    stop = asyncio.Event()
    install_signal_handlers(stop)
    metrics_task = asyncio.create_task(relay.run_metrics(stop))
    log.info("relay.started")
    try:
        await relay.run_forever(stop)
    finally:
        log.info("relay.stopping")
        stop.set()
        await metrics_task
        await publisher.close()
        await container.engine().dispose()
        container.shutdown_resources()


if __name__ == "__main__":
    asyncio.run(main())
