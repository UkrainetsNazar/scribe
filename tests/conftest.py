from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from dependency_injector import providers

from scribe.container import Container
from scribe.presentation.app import create_app
from tests.unit.fakes import FakeObjectStorage


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    container = Container()
    container.object_storage.override(providers.Object(FakeObjectStorage()))
    app = create_app(container)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
