import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pymongo import AsyncMongoClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from scribe.infrastructure.storage.minio_storage import MinioObjectStorage


class ReadinessChecker:
    """Checks the dependencies this process needs. The WebSocket service needs only
    PostgreSQL and Redis; the API also needs MongoDB and MinIO."""

    def __init__(
        self,
        engine: AsyncEngine,
        redis: Redis,
        mongo: AsyncMongoClient[dict[str, Any]],
        storage: MinioObjectStorage,
        role: str,
        timeout_sec: float = 2.0,
    ) -> None:
        self._engine = engine
        self._redis = redis
        self._mongo = mongo
        self._storage = storage
        self._role = role
        self._timeout = timeout_sec

    async def check(self) -> dict[str, str | None]:
        """Dependency -> None when it answers, or a short error description."""
        probes: dict[str, Callable[[], Awaitable[object]]] = {
            "postgres": self._postgres,
            "redis": self._redis_ping,
        }
        if self._role in ("all", "api"):
            probes["mongo"] = self._mongo_ping
            probes["minio"] = self._storage.ping
        outcomes = await asyncio.gather(*(self._run(probe) for probe in probes.values()))
        return dict(zip(probes, outcomes, strict=True))

    async def _run(self, probe: Callable[[], Awaitable[object]]) -> str | None:
        try:
            await asyncio.wait_for(probe(), timeout=self._timeout)
        except Exception as exc:
            return repr(exc)[:200] or "timeout"
        return None

    async def _postgres(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def _redis_ping(self) -> None:
        await self._redis.ping()

    async def _mongo_ping(self) -> None:
        await self._mongo.admin.command("ping")
