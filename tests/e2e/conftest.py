import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from tests.e2e.helpers import BASE_URL


@pytest.fixture(scope="session")
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as http:
        yield http


@pytest.fixture(scope="session", autouse=True)
async def stack_is_ready(client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + 120
    while True:
        try:
            if (await client.get("/health/ready")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, f"{BASE_URL}/health/ready did not return 200"
        await asyncio.sleep(2)
