from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager

from scribe.presentation.app import create_app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
