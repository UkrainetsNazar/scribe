import asyncio
import signal
from collections.abc import Awaitable, Callable

import structlog
from tenacity import AsyncRetrying, wait_exponential

log = structlog.get_logger()


async def connect_with_retry(connect: Callable[[], Awaitable[None]], name: str) -> None:
    """The broker may still be starting: retry forever with a capped exponential delay."""
    async for attempt in AsyncRetrying(wait=wait_exponential(multiplier=1, max=10)):
        with attempt:
            try:
                await connect()
            except Exception:
                log.warning("worker.connect_failed", worker=name)
                raise


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
