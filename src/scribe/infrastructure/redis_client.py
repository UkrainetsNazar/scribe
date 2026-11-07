from redis.asyncio import Redis


def create_redis(url: str) -> Redis:
    # Creation is synchronous; the app closes the client on shutdown
    return Redis.from_url(url, decode_responses=True)
