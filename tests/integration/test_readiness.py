from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from dependency_injector import providers

from scribe.config import Settings
from scribe.container import Container

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _container(settings: Settings) -> AsyncIterator[Container]:
    container = Container()
    container.settings.override(providers.Object(settings))
    try:
        yield container
    finally:
        await container.engine().dispose()
        await container.redis().aclose()
        await container.mongo().close()


async def test_every_dependency_is_reported(app_settings: Settings) -> None:
    async with _container(app_settings) as container:
        assert await container.readiness().check() == {
            "postgres": None,
            "redis": None,
            "mongo": None,
            "minio": None,
        }


async def test_a_broken_dependency_is_named_and_the_others_stay_ok(app_settings: Settings) -> None:
    broken = app_settings.model_copy(
        update={"mongo_url": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=500"}
    )
    async with _container(broken) as container:
        results = await container.readiness().check()
    assert results["mongo"] is not None
    assert [name for name, error in results.items() if error] == ["mongo"]
