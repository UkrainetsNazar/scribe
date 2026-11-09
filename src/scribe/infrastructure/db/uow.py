from types import TracebackType
from typing import Self

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.application.interfaces import UnitOfWork
from scribe.infrastructure.db.project_repositories import SqlAlchemyProjectRepository
from scribe.infrastructure.db.recording_repositories import SqlAlchemyRecordingRepository
from scribe.infrastructure.db.repositories import (
    SqlAlchemyPlanRepository,
    SqlAlchemyUserRepository,
)

log = structlog.get_logger()


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
            await self.session.close()
            self._session = None

    async def commit(self) -> None:
        await self.session.commit()
        for event in self.events:
            log.info("domain_event", event_type=type(event).__name__, event_id=str(event.event_id))
        self.events.clear()

    async def rollback(self) -> None:
        await self.session.rollback()
        self.events.clear()
