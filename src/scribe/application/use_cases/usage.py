from datetime import UTC, datetime
from uuid import UUID

from scribe.application.interfaces import UnitOfWorkFactory
from scribe.domain.quota import usage_period


class GetMonthlyUsage:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, user_id: UUID) -> int:
        async with self._uow_factory() as uow:
            return await uow.usage.get_minutes(user_id, usage_period(datetime.now(UTC)))
