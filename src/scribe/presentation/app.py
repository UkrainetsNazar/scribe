from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from scribe.container import Container
from scribe.presentation.routers import health

log = structlog.get_logger()


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
            container.shutdown_resources()

    app = FastAPI(title="Scribe API", version="1.0.0", lifespan=lifespan)
    app.state.container = container

    app.include_router(health.router)
    return app
