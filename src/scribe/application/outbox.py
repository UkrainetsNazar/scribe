from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from scribe.domain.events import DomainEvent


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    event_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    payload: dict[str, Any]
    headers: dict[str, Any]


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def to_outbox_record(event: DomainEvent, headers: Mapping[str, Any] | None = None) -> OutboxRecord:
    payload = {key: _jsonable(value) for key, value in asdict(event).items()}
    return OutboxRecord(
        event_id=event.event_id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        event_type=event.event_type,
        payload=payload,
        headers=dict(headers or {}),
    )
