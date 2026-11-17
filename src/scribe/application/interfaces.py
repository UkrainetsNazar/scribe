from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self
from uuid import UUID

from scribe.application.messaging import InboxRepository
from scribe.application.pagination import Cursor
from scribe.domain.events import DomainEvent
from scribe.domain.exports import Export, ExportFormat
from scribe.domain.pipeline import ProcessingStage, StageName
from scribe.domain.projects import Project
from scribe.domain.recordings import Recording, RecordingStatus
from scribe.domain.users import Plan, User

# ── Persistence ──────────────────────────────────────────────────────────────


class UserRepository(ABC):
    @abstractmethod
    async def add(self, user: User) -> None: ...

    @abstractmethod
    async def get_by_id(self, user_id: UUID) -> User | None: ...

    @abstractmethod
    async def get_by_email(self, email: str) -> User | None: ...


class PlanRepository(ABC):
    @abstractmethod
    async def get_by_name(self, name: str) -> Plan | None: ...


class ProjectRepository(ABC):
    @abstractmethod
    async def add(self, project: Project) -> None: ...

    @abstractmethod
    async def get(self, project_id: UUID, owner_id: UUID) -> Project | None: ...

    @abstractmethod
    async def get_by_name(self, owner_id: UUID, name: str) -> Project | None: ...

    @abstractmethod
    async def list_by_owner(
        self, owner_id: UUID, *, limit: int, cursor: Cursor | None
    ) -> list[Project]:
        """Returns up to limit + 1 rows, newest first."""

    @abstractmethod
    async def save(self, project: Project) -> None: ...


class RecordingRepository(ABC):
    @abstractmethod
    async def add(self, recording: Recording) -> None: ...

    @abstractmethod
    async def get(
        self, recording_id: UUID, owner_id: UUID, *, for_update: bool = False
    ) -> Recording | None: ...

    @abstractmethod
    async def list_by_owner(
        self,
        owner_id: UUID,
        *,
        project_id: UUID | None,
        status: RecordingStatus | None,
        limit: int,
        cursor: Cursor | None,
    ) -> list[Recording]:
        """Returns up to limit + 1 rows, newest first."""

    @abstractmethod
    async def filter_visible_ids(
        self, owner_id: UUID, recording_ids: Collection[UUID], project_id: UUID | None
    ) -> set[UUID]:
        """The given ids that belong to the owner, are completed and (optionally) in the project."""

    @abstractmethod
    async def purge(self, recording_id: UUID) -> bool:
        """Permanently removes a recording that is already marked deleted. Its stages and
        exports go with it (ON DELETE CASCADE). False if there was nothing to remove."""

    @abstractmethod
    async def save(self, recording: Recording) -> None: ...


class StageRepository(ABC):
    @abstractmethod
    async def reset_incomplete(self, recording_id: UUID) -> None:
        """Creates missing stages and sets every non-completed stage back to pending."""

    @abstractmethod
    async def get(self, recording_id: UUID, name: StageName) -> ProcessingStage | None: ...

    @abstractmethod
    async def list_for_recording(self, recording_id: UUID) -> list[ProcessingStage]:
        """In pipeline order."""

    @abstractmethod
    async def save(self, stage: ProcessingStage) -> None: ...


class UsageRepository(ABC):
    @abstractmethod
    async def lock_minutes(self, user_id: UUID, period: str) -> int:
        """Creates the row if needed and locks it until the transaction ends."""

    @abstractmethod
    async def add_minutes(self, user_id: UUID, period: str, minutes: int) -> None: ...

    @abstractmethod
    async def get_minutes(self, user_id: UUID, period: str) -> int: ...


class ExportRepository(ABC):
    @abstractmethod
    async def upsert(self, export: Export) -> None:
        """Insert, or update the existing (recording, format) row; its id is kept."""

    @abstractmethod
    async def get(self, recording_id: UUID, fmt: ExportFormat) -> Export | None: ...


class UnitOfWork(ABC):
    users: UserRepository
    plans: PlanRepository
    projects: ProjectRepository
    recordings: RecordingRepository
    inbox: InboxRepository
    stages: StageRepository
    usage: UsageRepository
    exports: ExportRepository

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    def add_events(self, events: Iterable[DomainEvent]) -> None:
        self.events.extend(events)

    @abstractmethod
    async def __aenter__(self) -> Self: ...

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]

# ── Security ─────────────────────────────────────────────────────────────────


class PasswordHasher(ABC):
    @property
    @abstractmethod
    def dummy_hash(self) -> str:
        """Dummy hash: verifying against it evens out response time for unknown emails."""

    @abstractmethod
    async def hash(self, password: str) -> str: ...

    @abstractmethod
    async def verify(self, password: str, hashed: str) -> bool: ...


TokenType = Literal["access", "refresh"]


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    jti: str
    ttl_seconds: int


@dataclass(frozen=True, slots=True)
class TokenClaims:
    sub: UUID
    jti: str
    type: TokenType
    expires_at: datetime


class TokenService(ABC):
    @abstractmethod
    def create_access_token(self, user_id: UUID) -> IssuedToken: ...

    @abstractmethod
    def create_refresh_token(self, user_id: UUID) -> IssuedToken: ...

    @abstractmethod
    def decode(self, token: str, expected: TokenType) -> TokenClaims:
        """Raises TokenExpiredError or UnauthorizedError."""


class RefreshTokenStore(ABC):
    @abstractmethod
    async def save(self, jti: str, user_id: UUID, ttl_seconds: int) -> None: ...

    @abstractmethod
    async def consume(self, jti: str) -> UUID | None:
        """Atomically reads and deletes. None if the token was revoked or already used."""

    @abstractmethod
    async def revoke(self, jti: str) -> None: ...


# ── Storage and media ────────────────────────────────────────────────────────


class ObjectModifiedError(Exception):
    """The object changed after it had been checked (its ETag no longer matches)."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    size: int
    content_type: str | None
    etag: str


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration_sec: float


class ObjectStorage(ABC):
    @abstractmethod
    async def ensure_buckets(self) -> None: ...

    @abstractmethod
    async def create_upload_url(self, bucket: str, key: str, ttl_seconds: int) -> str:
        """Presigned PUT URL for clients (uses the public endpoint)."""

    @abstractmethod
    async def create_internal_read_url(self, bucket: str, key: str, ttl_seconds: int) -> str:
        """Presigned GET URL for our own processes (uses the internal endpoint)."""

    @abstractmethod
    async def stat(self, bucket: str, key: str) -> StoredObject | None:
        """None if the object does not exist."""

    @abstractmethod
    async def upload_file(self, bucket: str, key: str, path: Path, content_type: str) -> None: ...

    @abstractmethod
    async def download_file(self, bucket: str, key: str, path: Path) -> None:
        """Raises FileNotFoundError if the object does not exist."""

    @abstractmethod
    async def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    async def get_bytes(self, bucket: str, key: str) -> bytes | None:
        """None if the object does not exist."""

    @abstractmethod
    async def create_download_url(
        self, bucket: str, key: str, ttl_seconds: int, filename: str
    ) -> str:
        """Presigned GET URL for clients; the browser saves the file as `filename`."""

    @abstractmethod
    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        """Deletes every object whose key starts with `prefix`. Returns how many were removed."""

    @abstractmethod
    async def copy(
        self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str, *, expected_etag: str
    ) -> None:
        """Server-side copy of exactly the version with this ETag.
        Raises ObjectModifiedError if the source changed, FileNotFoundError if it is gone."""


class MediaProbe(ABC):
    @abstractmethod
    async def probe(self, source_url: str, content_type: str) -> MediaInfo:
        """Raises InvalidMediaError if the file is not valid media with an audio track."""
