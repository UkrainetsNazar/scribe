import pytest
from starlette.requests import Request

from scribe.config import Settings
from scribe.domain.errors import RateLimitedError
from scribe.domain.users import User
from scribe.presentation.deps import _enforce_rate_limit
from tests.unit.fakes import FREE_PLAN, FakeRateLimiter

SETTINGS = Settings(
    _env_file=None,
    rate_limit_per_minute=300,
    rate_limit_heavy_per_minute=60,
    rate_limit_window_sec=60,
)
USER = User.register("a@example.com", "hash", FREE_PLAN)
UID = "11111111-1111-1111-1111-111111111111"


def _request(method: str, path: str) -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": []})


async def test_requests_are_counted_per_user_and_route() -> None:
    limiter = FakeRateLimiter()
    await _enforce_rate_limit(_request("GET", f"/api/v1/recordings/{UID}"), USER, limiter, SETTINGS)
    assert limiter.calls == [(f"rl:{USER.id}:GET /api/v1/recordings/{{id}}", 300, 60)]


async def test_expensive_routes_use_the_stricter_limit() -> None:
    limiter = FakeRateLimiter()
    await _enforce_rate_limit(_request("GET", "/api/v1/search"), USER, limiter, SETTINGS)
    await _enforce_rate_limit(
        _request("POST", f"/api/v1/recordings/{UID}/complete-upload"), USER, limiter, SETTINGS
    )
    assert [limit for _, limit, _ in limiter.calls] == [60, 60]


async def test_a_blocked_request_carries_the_retry_delay() -> None:
    limiter = FakeRateLimiter(allowed=False, retry_after_sec=7)
    with pytest.raises(RateLimitedError) as blocked:
        await _enforce_rate_limit(_request("GET", "/api/v1/users/me"), USER, limiter, SETTINGS)
    assert blocked.value.retry_after_sec == 7


async def test_an_unavailable_limiter_does_not_block_requests() -> None:
    limiter = FakeRateLimiter(error=ConnectionError("redis is down"))
    await _enforce_rate_limit(_request("GET", "/api/v1/users/me"), USER, limiter, SETTINGS)
