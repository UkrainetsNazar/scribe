from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    event_type: ClassVar[str]
    aggregate_type: ClassVar[str]

    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def aggregate_id(self) -> UUID:
        raise NotImplementedError


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingEvent(DomainEvent):
    aggregate_type: ClassVar[str] = "recording"

    recording_id: UUID
    owner_id: UUID

    @property
    def aggregate_id(self) -> UUID:
        return self.recording_id


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingCreated(RecordingEvent):
    event_type: ClassVar[str] = "recording.created"

    project_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingUploaded(RecordingEvent):
    event_type: ClassVar[str] = "recording.uploaded"


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingProcessingRequested(RecordingEvent):
    event_type: ClassVar[str] = "recording.processing_requested"

    storage_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingCompleted(RecordingEvent):
    event_type: ClassVar[str] = "recording.completed"


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingFailed(RecordingEvent):
    event_type: ClassVar[str] = "recording.failed"

    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingCancelled(RecordingEvent):
    event_type: ClassVar[str] = "recording.cancelled"


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordingDeleted(RecordingEvent):
    event_type: ClassVar[str] = "recording.deleted"

    storage_key: str
