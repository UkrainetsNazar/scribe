import httpx
from asgi_lifespan import LifespanManager
from dependency_injector import providers

from scribe.container import Container
from scribe.presentation.app import create_app
from tests.unit.fakes import FakeObjectStorage


class StubChecker:
    def __init__(self, results: dict[str, str | None]) -> None:
        self._results = results

    async def check(self) -> dict[str, str | None]:
        return self._results


async def _ready(results: dict[str, str | None]) -> httpx.Response:
    container = Container()
    container.object_storage.override(providers.Object(FakeObjectStorage()))
    container.readiness.override(providers.Object(StubChecker(results)))
    app = create_app(container)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health/ready")


async def test_ready_when_every_dependency_answers() -> None:
    resp = await _ready({"postgres": None, "redis": None})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": ["postgres", "redis"]}


async def test_not_ready_names_the_failed_dependency_but_hides_details() -> None:
    resp = await _ready({"postgres": None, "mongo": "ConnectionError('secret-host:27017')"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "mongo" in body["detail"]
    assert "secret-host" not in resp.text
