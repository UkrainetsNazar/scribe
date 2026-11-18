from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError


def create_redis(url: str) -> Redis:
    # Creation is synchronous; the owning process closes the client on shutdown.
    # Retries make the first command after a Redis restart succeed on a fresh connection.
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
        health_check_interval=15,
        retry=Retry(ExponentialBackoff(cap=2, base=0.1), 3),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )
