from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from scribe.container import Container
from scribe.presentation.errors import register_exception_handlers
from scribe.presentation.middleware import TraceIdMiddleware
from scribe.presentation.routers import auth, health, users

log = structlog.get_logger()

API_PREFIX = "/api/v1"


def create_app(container: Container | None = None) -> FastAPI:
    container = container or Container()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        container.init_resources()
        log.info("app.startup", env=container.settings().app_env)
        try:
            yield
        finally:
            log.info("app.shutdown")
            await container.engine().dispose()
            await container.redis().aclose()
            container.shutdown_resources()
            container.unwire()

    app = FastAPI(title="Scribe API", version="1.0.0", lifespan=lifespan)
    app.state.container = container

    app.add_middleware(TraceIdMiddleware)
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(users.router, prefix=API_PREFIX)
    return app
