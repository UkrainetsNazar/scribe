import asyncio
import contextlib

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.application.messaging import MessagePublisher
from scribe.infrastructure.db.messaging_repositories import SqlAlchemyOutboxRepository

log = structlog.get_logger()


class OutboxRelay:
    """Moves events from the outbox table to the broker (at-least-once)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: MessagePublisher,
        batch_size: int = 100,
        poll_interval_sec: float = 0.2,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._batch_size = batch_size
        self._poll_interval = poll_interval_sec

    async def run_once(self) -> int:
        """Publishes one batch and returns how many events were published.

        If the broker fails midway, the events published so far are still marked and the error
        is raised afterwards. A crash between publish and commit republishes the batch later;
        the consumer-side inbox discards such duplicates.
        """
        failure: Exception | None = None
        published: list[int] = []
        async with self._session_factory() as session:
            repo = SqlAlchemyOutboxRepository(session)
            batch = await repo.fetch_unpublished(self._batch_size)  # FOR UPDATE SKIP LOCKED
            for message in batch:
                try:
                    await self._publisher.publish(message)
                except Exception as exc:
                    failure = exc
                    break  # stop at the first failure to keep the order
                published.append(message.id)
            if published:
                await repo.mark_published(published)
                await session.commit()
        if failure is not None:
            raise failure
        return len(published)

    async def run_forever(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                count = await self.run_once()
                backoff = 1.0
            except Exception:
                log.exception("outbox.relay_error", retry_in_sec=backoff)
                await self._sleep(stop, backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            if count:
                log.info("outbox.published", count=count)
            else:
                await self._sleep(stop, self._poll_interval)

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
