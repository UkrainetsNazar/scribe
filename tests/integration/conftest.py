import asyncio
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from alembic import command
from asgi_lifespan import LifespanManager
from dependency_injector import providers
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from scribe.config import Settings
from scribe.container import Container
from scribe.presentation.app import create_app
from tests.integration.helpers import alembic_config


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer("redis:7-alpine") as redis:
        host = redis.get_container_host_ip()
        port = redis.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
async def migrated_db(postgres_url: str) -> str:
    await asyncio.to_thread(command.upgrade, alembic_config(postgres_url), "head")
    return postgres_url


@pytest.fixture
async def api_client(migrated_db: str, redis_url: str) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(database_url=migrated_db, redis_url=redis_url, log_json=False)
    container = Container()
    container.settings.override(providers.Object(settings))
    app = create_app(container)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
