from collections.abc import AsyncIterator

from redis.asyncio import Redis


async def init_redis(url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()
