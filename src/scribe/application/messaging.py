from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: int
    event_id: UUID
    event_type: str
    payload: dict[str, Any]
    headers: dict[str, Any]
    created_at: datetime


class MessagePublisher(ABC):
    @abstractmethod
    async def publish(self, message: OutboxMessage) -> None:
        """Returns only after the broker confirmed the message. Raises on failure."""


class InboxRepository(ABC):
    @abstractmethod
    async def register(self, message_id: UUID, consumer: str) -> bool:
        """Records the message for the consumer. False if it was already registered."""

    @abstractmethod
    async def mark_processed(self, message_id: UUID, consumer: str) -> None: ...
