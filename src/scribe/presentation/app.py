from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from tenacity import AsyncRetrying, stop_after_attempt, wait_fixed

from scribe.container import Container
from scribe.infrastructure.telemetry import setup_tracing
from scribe.presentation.errors import register_exception_handlers
from scribe.presentation.http_metrics import HttpMetricsMiddleware
from scribe.presentation.middleware import TraceIdMiddleware
from scribe.presentation.routers import (
    auth,
    health,
    internal,
    metrics,
    projects,
    recordings,
    results,
    users,
    webhooks,
    ws,
)

log = structlog.get_logger()

API_PREFIX = "/api/v1"


async def _ensure_buckets(container: Container) -> None:
    """MinIO may still be starting: retry for ~15 seconds."""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(15), wait=wait_fixed(1), reraise=True
    ):
        with attempt:
            await container.object_storage().ensure_buckets()


def _skip_telemetry(scope: Mapping[str, Any]) -> bool:
    """Health checks, scrapes and the gateway's auth calls would drown the useful traces."""
    return str(scope.get("path", "")).startswith(("/health/", "/metrics", "/internal/"))


def create_app(container: Container | None = None) -> FastAPI:
    container = container or Container()
    container.wire()  # uses wiring_config (auto_wire is off)

    settings = container.settings()
    role = settings.app_role
    serves_rest = role in ("all", "api")
    serves_ws = role in ("all", "ws")
    setup_tracing(settings)  # before FastAPI(): the app resolves the global provider

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        container.init_resources()
        if serves_rest:  # the WebSocket service needs only PostgreSQL and Redis
            await _ensure_buckets(container)
        log.info("app.startup", env=settings.app_env, role=role)
        try:
            yield
        finally:
            log.info("app.shutdown")
            await container.engine().dispose()
            await container.redis().aclose()
            await container.mongo().close()
            container.shutdown_resources()
            container.unwire()

    app = FastAPI(
        title="Scribe API",
        version="1.0.0",
        lifespan=lifespan,
        # Spans for HTTP and WebSocket requests only; we export through our own provider and
        # collect metrics ourselves (Prometheus), so FastAPI's metrics and logs are off
        telemetry={
            "auto_configure": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "exclude": _skip_telemetry,
        },
    )
    app.state.container = container

    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(HttpMetricsMiddleware)  # outermost of ours: also sees error responses
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(metrics.router)
    if serves_rest:
        for router in (
            auth.router,
            users.router,
            projects.router,
            webhooks.router,
            recordings.router,
            results.router,
        ):
            app.include_router(router, prefix=API_PREFIX)
        app.include_router(internal.router)
    if serves_ws:
        app.include_router(ws.router)
    return app
