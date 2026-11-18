import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from asgi_lifespan import LifespanManager
from dependency_injector import providers
from redis.asyncio import Redis

from scribe.config import Settings
from scribe.container import Container
from scribe.infrastructure.rate_limit import RedisRateLimiter
from scribe.presentation.app import create_app
from tests.integration.helpers import API, auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


async def test_the_window_slides(redis: Redis) -> None:
    limiter = RedisRateLimiter(redis)
    key = f"rl:test:{uuid4().hex}"

    results = [await limiter.hit(key, 3, 1) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[3].retry_after_sec == 1
    assert (await limiter.hit(f"{key}:other", 3, 1)).allowed  # keys are independent

    await asyncio.sleep(1.1)
    assert (await limiter.hit(key, 3, 1)).allowed  # the oldest requests left the window


@pytest.fixture
async def limited_client(app_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    settings = app_settings.model_copy(
        update={"rate_limit_per_minute": 5, "rate_limit_heavy_per_minute": 2}
    )
    container = Container()
    container.settings.override(providers.Object(settings))
    app = create_app(container)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_the_api_limits_requests_per_user_and_route(
    limited_client: httpx.AsyncClient,
) -> None:
    first = await auth_headers(limited_client)
    other = await auth_headers(limited_client)
    me = f"{API}/users/me"

    for _ in range(5):
        assert (await limited_client.get(me, headers=first)).status_code == 200
    blocked = await limited_client.get(me, headers=first)
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "RATE_LIMITED"
    assert blocked.headers["content-type"].startswith("application/problem+json")
    assert int(blocked.headers["retry-after"]) >= 1

    assert (await limited_client.get(me, headers=other)).status_code == 200  # another user
    assert (
        await limited_client.get(f"{API}/projects", headers=first)
    ).status_code == 200  # another route


async def test_expensive_routes_have_a_stricter_limit(limited_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(limited_client)
    statuses = [
        (await limited_client.get(f"{API}/search?q=ab", headers=headers)).status_code
        for _ in range(3)
    ]
    assert statuses == [200, 200, 429]
