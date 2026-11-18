import math
import uuid

from redis.asyncio import Redis

from scribe.application.interfaces import RateLimitDecision, RateLimiter

# Sorted set of request timestamps per key. The time comes from Redis, so API instances with
# different clocks agree. KEYS: key. ARGV: window in ms, limit, a unique member.
_SLIDING_WINDOW = """
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
if redis.call('ZCARD', KEYS[1]) < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[3])
  redis.call('PEXPIRE', KEYS[1], window)
  return {1, 0}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {0, tonumber(oldest[2]) + window - now}
"""


class RedisRateLimiter(RateLimiter):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, key: str, limit: int, window_sec: int) -> RateLimitDecision:
        allowed, retry_ms = await self._redis.eval(
            _SLIDING_WINDOW, 1, key, window_sec * 1000, limit, uuid.uuid4().hex
        )
        if int(allowed):
            return RateLimitDecision(allowed=True)
        return RateLimitDecision(
            allowed=False, retry_after_sec=max(1, math.ceil(int(retry_ms) / 1000))
        )
