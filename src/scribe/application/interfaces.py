from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Literal, Self
from uuid import UUID

from scribe.application.messaging import InboxRepository
from scribe.application.pagination import Cursor
from scribe.domain.events import DomainEvent
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
    async def save(self, recording: Recording) -> None: ...


class UnitOfWork(ABC):
    users: UserRepository
    plans: PlanRepository
    projects: ProjectRepository
    recordings: RecordingRepository
    inbox: InboxRepository

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


@dataclass(frozen=True, slots=True)
class StoredObject:
    size: int
    content_type: str | None


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


class MediaProbe(ABC):
    @abstractmethod
    async def probe(self, source_url: str) -> MediaInfo:
        """Raises InvalidMediaError if the file is not valid media with an audio track."""
