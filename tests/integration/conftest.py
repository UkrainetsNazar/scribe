import asyncio
import socket
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import httpx
import pytest
from alembic import command
from asgi_lifespan import LifespanManager
from dependency_injector import providers
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from scribe.config import Settings
from scribe.container import Container
from scribe.presentation.app import create_app
from tests.integration.helpers import alembic_config

# Keep in sync with docker-compose.yml
MINIO_IMAGE = "docker.io/pgsty/silo:RELEASE.2026-10-26T00-00-00Z"
MINIO_USER = "scribe"
MINIO_PASSWORD = "scribe-secret"
RABBIT_IMAGE = "rabbitmq:3.13-management-alpine"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class RabbitBroker:
    url: str
    mgmt_port: int
    container: DockerContainer

    def stop(self) -> None:
        self.container.get_wrapped_container().stop()

    def start(self) -> None:
        self.container.get_wrapped_container().start()

    def wait_ready(self, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        health = f"http://127.0.0.1:{self.mgmt_port}/api/health/checks/port-listener/5672"
        while True:
            try:
                if httpx.get(health, auth=("scribe", "scribe"), timeout=3).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("RabbitMQ did not become ready in time")
            time.sleep(1)


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
def minio_endpoint() -> Iterator[str]:
    container = (
        DockerContainer(MINIO_IMAGE)
        .with_command("server /data")
        .with_env("MINIO_ROOT_USER", MINIO_USER)
        .with_env("MINIO_ROOT_PASSWORD", MINIO_PASSWORD)
        .with_exposed_ports(9000)
    )
    with container:
        endpoint = f"{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        health_url = f"http://{endpoint}/minio/health/live"
        for _ in range(60):
            try:
                if httpx.get(health_url, timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("MinIO did not become ready in time")
        yield endpoint


@pytest.fixture(scope="session")
def rabbit() -> Iterator[RabbitBroker]:
    # Fixed host ports: they must survive a stop/start of the container
    amqp_port, mgmt_port = _free_port(), _free_port()
    container = (
        DockerContainer(RABBIT_IMAGE)
        .with_bind_ports(5672, amqp_port)
        .with_bind_ports(15672, mgmt_port)
        .with_env("RABBITMQ_DEFAULT_USER", "scribe")
        .with_env("RABBITMQ_DEFAULT_PASS", "scribe")
    )
    with container:
        broker = RabbitBroker(
            url=f"amqp://scribe:scribe@127.0.0.1:{amqp_port}/",
            mgmt_port=mgmt_port,
            container=container,
        )
        broker.wait_ready()
        yield broker


@pytest.fixture(scope="session")
async def migrated_db(postgres_url: str) -> str:
    # alembic env.py calls asyncio.run(), so run it in a separate thread
    await asyncio.to_thread(command.upgrade, alembic_config(postgres_url), "head")
    return postgres_url


@pytest.fixture
async def session_factory(migrated_db: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def api_client(
    migrated_db: str, redis_url: str, minio_endpoint: str
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url=migrated_db,
        redis_url=redis_url,
        log_json=False,
        minio_endpoint=minio_endpoint,
        minio_public_endpoint=minio_endpoint,
        minio_access_key=MINIO_USER,
        minio_secret_key=MINIO_PASSWORD,  # type: ignore[arg-type]
    )
    container = Container()
    container.settings.override(providers.Object(settings))
    app = create_app(container)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
