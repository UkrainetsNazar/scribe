import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from scribe.domain.errors import (
    DomainValidationError,
    FileTooLargeError,
    InvalidStateError,
    UnsupportedMediaError,
)
from scribe.domain.events import (
    DomainEvent,
    RecordingCancelled,
    RecordingCompleted,
    RecordingCreated,
    RecordingDeleted,
    RecordingFailed,
    RecordingProcessingRequested,
    RecordingUploaded,
)
from scribe.domain.users import Plan

MAX_FILE_BYTES = 2 * 1024**3
MAX_DURATION_SEC = 4 * 60 * 60
MAX_TITLE_LENGTH = 200

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "audio/mpeg",
        "audio/wav",
        "audio/mp4",
        "audio/ogg",
        "audio/flac",
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
)

_LANGUAGE_RE = re.compile(r"[a-z]{2}")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class RecordingStatus(StrEnum):
    CREATED = "created"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELETED = "deleted"


S = RecordingStatus

ALLOWED_TRANSITIONS: dict[RecordingStatus, frozenset[RecordingStatus]] = {
    S.CREATED: frozenset({S.UPLOADED, S.DELETED}),
    S.UPLOADED: frozenset({S.PROCESSING, S.FAILED}),
    S.PROCESSING: frozenset({S.COMPLETED, S.FAILED, S.CANCELLED}),
    S.FAILED: frozenset({S.PROCESSING, S.DELETED}),
    S.CANCELLED: frozenset({S.PROCESSING, S.DELETED}),
    S.COMPLETED: frozenset({S.DELETED}),
    S.DELETED: frozenset(),
}


def safe_filename(filename: str) -> str:
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", base)[:100].lstrip(".")
    return cleaned or "source"


def build_storage_key(owner_id: UUID, recording_id: UUID, filename: str) -> str:
    return f"{owner_id}/{recording_id}/source/{safe_filename(filename)}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Recording:
    id: UUID
    project_id: UUID
    owner_id: UUID
    title: str
    source_filename: str
    content_type: str
    size_bytes: int
    language: str
    storage_key: str
    status: RecordingStatus = RecordingStatus.CREATED
    duration_sec: float | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    events: list[DomainEvent] = field(default_factory=list, init=False, repr=False, compare=False)

    @classmethod
    def create(
        cls,
        *,
        project_id: UUID,
        owner_id: UUID,
        title: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        language: str,
        plan: Plan,
    ) -> Self:
        title = title.strip()
        if not 1 <= len(title) <= MAX_TITLE_LENGTH:
            raise DomainValidationError(f"Title must be 1-{MAX_TITLE_LENGTH} characters")
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise UnsupportedMediaError(f"Content type '{content_type}' is not supported")
        if size_bytes < 1:
            raise DomainValidationError("size_bytes must be positive")
        if size_bytes > min(plan.max_file_mb * 1024 * 1024, MAX_FILE_BYTES):
            raise FileTooLargeError(
                f"File exceeds the {plan.max_file_mb} MB limit of the '{plan.name}' plan"
            )
        language = language.strip().lower()
        if language != "auto" and not _LANGUAGE_RE.fullmatch(language):
            raise DomainValidationError("language must be 'auto' or an ISO 639-1 code")

        recording_id = uuid4()
        recording = cls(
            id=recording_id,
            project_id=project_id,
            owner_id=owner_id,
            title=title,
            source_filename=filename.strip()[:255] or "source",
            content_type=content_type,
            size_bytes=size_bytes,
            language=language,
            storage_key=build_storage_key(owner_id, recording_id, filename),
        )
        recording.events.append(
            RecordingCreated(recording_id=recording_id, owner_id=owner_id, project_id=project_id)
        )
        return recording

    def _move(self, target: RecordingStatus) -> None:
        if target not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidStateError(f"Cannot move recording from '{self.status}' to '{target}'")
        self.status = target
        self.updated_at = _now()

    def mark_uploaded(self) -> None:
        self._move(S.UPLOADED)
        self.events.append(RecordingUploaded(recording_id=self.id, owner_id=self.owner_id))

    def start_processing(self, duration_sec: float) -> None:
        if self.status is not S.UPLOADED:
            raise InvalidStateError(f"Cannot start processing from '{self.status}'")
        self._move(S.PROCESSING)
        self.duration_sec = duration_sec
        self._request_processing()

    def retry(self) -> None:
        if self.status not in (S.FAILED, S.CANCELLED):
            raise InvalidStateError(f"Cannot retry a recording in '{self.status}'")
        self._move(S.PROCESSING)
        self._request_processing()

    def complete(self) -> None:
        self._move(S.COMPLETED)
        self.events.append(RecordingCompleted(recording_id=self.id, owner_id=self.owner_id))

    def fail(self, reason: str) -> None:
        self._move(S.FAILED)
        self.events.append(
            RecordingFailed(recording_id=self.id, owner_id=self.owner_id, reason=reason)
        )

    def cancel(self) -> None:
        self._move(S.CANCELLED)
        self.events.append(RecordingCancelled(recording_id=self.id, owner_id=self.owner_id))

    def delete(self) -> None:
        self._move(S.DELETED)
        self.events.append(
            RecordingDeleted(
                recording_id=self.id, owner_id=self.owner_id, storage_key=self.storage_key
            )
        )

    def _request_processing(self) -> None:
        self.events.append(
            RecordingProcessingRequested(
                recording_id=self.id, owner_id=self.owner_id, storage_key=self.storage_key
            )
        )

    def pull_events(self) -> list[DomainEvent]:
        events, self.events = self.events, []
        return events
