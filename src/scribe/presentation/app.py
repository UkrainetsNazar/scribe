from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from tenacity import AsyncRetrying, stop_after_attempt, wait_fixed

from scribe.container import Container
from scribe.presentation.errors import register_exception_handlers
from scribe.presentation.middleware import TraceIdMiddleware
from scribe.presentation.routers import (
    auth,
    health,
    internal,
    projects,
    recordings,
    results,
    users,
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


def create_app(container: Container | None = None) -> FastAPI:
    container = container or Container()
    container.wire()  # uses wiring_config (auto_wire is off)

    role = container.settings().app_role
    serves_rest = role in ("all", "api")
    serves_ws = role in ("all", "ws")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        container.init_resources()
        if serves_rest:  # the WebSocket service needs only PostgreSQL and Redis
            await _ensure_buckets(container)
        log.info("app.startup", env=container.settings().app_env, role=role)
        try:
            yield
        finally:
            log.info("app.shutdown")
            await container.engine().dispose()
            await container.redis().aclose()
            if serves_rest:
                await container.mongo().close()
            container.shutdown_resources()
            container.unwire()

    app = FastAPI(title="Scribe API", version="1.0.0", lifespan=lifespan)
    app.state.container = container

    app.add_middleware(TraceIdMiddleware)
    register_exception_handlers(app)

    app.include_router(health.router)
    if serves_rest:
        for router in (
            auth.router,
            users.router,
            projects.router,
            recordings.router,
            results.router,
        ):
            app.include_router(router, prefix=API_PREFIX)
        app.include_router(internal.router)
    if serves_ws:
        app.include_router(ws.router)
    return app
