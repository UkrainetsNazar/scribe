from types import TracebackType
from typing import Any, Self

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.application.interfaces import UnitOfWork
from scribe.application.outbox import to_outbox_record
from scribe.infrastructure.db.export_repositories import SqlAlchemyExportRepository
from scribe.infrastructure.db.messaging_repositories import (
    SqlAlchemyInboxRepository,
    SqlAlchemyOutboxRepository,
)
from scribe.infrastructure.db.pipeline_repositories import (
    SqlAlchemyStageRepository,
    SqlAlchemyUsageRepository,
)
from scribe.infrastructure.db.project_repositories import SqlAlchemyProjectRepository
from scribe.infrastructure.db.recording_repositories import SqlAlchemyRecordingRepository
from scribe.infrastructure.db.repositories import (
    SqlAlchemyPlanRepository,
    SqlAlchemyUserRepository,
)


def _trace_headers() -> dict[str, Any]:
    trace_id = structlog.contextvars.get_contextvars().get("trace_id")
    return {"trace_id": trace_id} if trace_id else {}


class SqlAlchemyUnitOfWork(UnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork is not active: use 'async with'")
        return self._session

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self.users = SqlAlchemyUserRepository(self._session)
        self.plans = SqlAlchemyPlanRepository(self._session)
        self.projects = SqlAlchemyProjectRepository(self._session)
        self.recordings = SqlAlchemyRecordingRepository(self._session)
        self.inbox = SqlAlchemyInboxRepository(self._session)
        self.outbox = SqlAlchemyOutboxRepository(self._session)
        self.stages = SqlAlchemyStageRepository(self._session)
        self.usage = SqlAlchemyUsageRepository(self._session)
        self.exports = SqlAlchemyExportRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                await self.rollback()
        finally:
            await self.session.close()  # without commit() everything is rolled back
            self._session = None

    async def commit(self) -> None:
        # Transactional Outbox: events are stored in the same transaction as the aggregate
        headers = _trace_headers()
        await self.outbox.add_many([to_outbox_record(e, headers) for e in self.events])
        await self.session.commit()
        self.events.clear()

    async def rollback(self) -> None:
        await self.session.rollback()
        self.events.clear()
