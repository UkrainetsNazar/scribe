from uuid import UUID

from redis.asyncio import Redis

from scribe.application.interfaces import RefreshTokenStore


class RedisRefreshTokenStore(RefreshTokenStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(jti: str) -> str:
        return f"refresh:{jti}"

    async def save(self, jti: str, user_id: UUID, ttl_seconds: int) -> None:
        await self._redis.set(self._key(jti), str(user_id), ex=ttl_seconds)

    async def consume(self, jti: str) -> UUID | None:
        raw = await self._redis.getdel(self._key(jti))  # atomic: read + delete
        if not raw:
            return None
        return UUID(raw.decode() if isinstance(raw, bytes) else raw)

    async def revoke(self, jti: str) -> None:
        await self._redis.delete(self._key(jti))
