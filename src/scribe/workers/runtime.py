from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from scribe.container import Container


@asynccontextmanager
async def worker_container() -> AsyncGenerator[Container]:
    """A fresh container per task. Every task runs in its own event loop (asyncio.run), and
    async clients (SQLAlchemy pool, Mongo) must not outlive the loop they were created in."""
    container = Container()
    container.init_resources()
    try:
        yield container
    finally:
        await container.engine().dispose()
        await container.redis().aclose()
        await container.mongo().close()
        container.shutdown_resources()
