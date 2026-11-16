import asyncio
import signal
from collections.abc import Awaitable, Callable

import structlog
from prometheus_client import start_http_server
from tenacity import AsyncRetrying, wait_exponential

from scribe.config import Settings
from scribe.infrastructure.telemetry import setup_tracing

log = structlog.get_logger()


def init_observability(settings: Settings) -> None:
    """Tracing and, when METRICS_PORT is set, a Prometheus exporter for a single-process worker."""
    setup_tracing(settings)
    if settings.metrics_port is not None:
        start_http_server(settings.metrics_port)


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
