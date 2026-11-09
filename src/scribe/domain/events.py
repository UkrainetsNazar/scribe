from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingCreated(DomainEvent):
    recording_id: UUID
    owner_id: UUID
    project_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingUploaded(DomainEvent):
    recording_id: UUID
    owner_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingProcessingRequested(DomainEvent):
    recording_id: UUID
    owner_id: UUID
    storage_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingCompleted(DomainEvent):
    recording_id: UUID
    owner_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingFailed(DomainEvent):
    recording_id: UUID
    owner_id: UUID
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingCancelled(DomainEvent):
    recording_id: UUID
    owner_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingDeleted(DomainEvent):
    recording_id: UUID
    owner_id: UUID
    storage_key: str
