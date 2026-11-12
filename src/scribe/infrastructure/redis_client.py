from redis.asyncio import Redis


def create_redis(url: str) -> Redis:
    # Creation is synchronous; the owning process closes the client on shutdown
    return Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=10)
