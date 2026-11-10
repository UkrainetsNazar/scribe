from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from scribe.application.interfaces import UnitOfWork, UnitOfWorkFactory


class PermanentHandlerError(Exception):
    """Raised by a handler when retrying cannot help: the message goes straight to the DLQ."""


@dataclass(frozen=True, slots=True)
class InboundEvent:
    event_id: UUID
    event_type: str
    payload: dict[str, Any]
    headers: dict[str, Any]


class EventHandler(ABC):
    @abstractmethod
    async def handle(self, event: InboundEvent, uow: UnitOfWork) -> None:
        """Runs inside the inbox transaction."""


class InboxGuard:
    """Makes a consumer idempotent.

    The inbox row, the handler's DB changes and the commit share one transaction:
    - duplicate: the row already exists, the handler is skipped;
    - concurrent duplicates: the second INSERT waits for the first transaction to finish;
    - handler failure: everything rolls back, including the inbox row, so a retry runs again.
    """

    def __init__(
        self, uow_factory: UnitOfWorkFactory, consumer: str, handler: EventHandler
    ) -> None:
        self._uow_factory = uow_factory
        self._consumer = consumer
        self._handler = handler

    async def process(self, event: InboundEvent) -> bool:
        async with self._uow_factory() as uow:
            if not await uow.inbox.register(event.event_id, self._consumer):
                return False
            await self._handler.handle(event, uow)
            await uow.inbox.mark_processed(event.event_id, self._consumer)
            await uow.commit()
        return True
