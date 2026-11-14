from typing import Literal

import httpx
import pytest
from asgi_lifespan import LifespanManager
from dependency_injector import providers

from scribe.config import Settings
from scribe.container import Container
from scribe.presentation.app import create_app
from tests.unit.fakes import FakeObjectStorage

Role = Literal["all", "api", "ws"]


async def _status_codes(role: Role, paths: list[str]) -> list[int]:
    container = Container()
    container.settings.override(providers.Object(Settings(app_role=role, log_json=False)))
    container.object_storage.override(providers.Object(FakeObjectStorage()))
    app = create_app(container)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return [(await client.get(path)).status_code for path in paths]


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("all", [200, 401, 401]),
        ("api", [200, 401, 401]),
        ("ws", [200, 404, 404]),  # no REST routes and no forward-auth endpoint
    ],
)
async def test_role_decides_which_routes_exist(role: Role, expected: list[int]) -> None:
    paths = ["/health/live", "/api/v1/users/me", "/internal/auth"]
    assert await _status_codes(role, paths) == expected
