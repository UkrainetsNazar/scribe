from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Literal, Self
from uuid import UUID

from scribe.domain.users import Plan, User


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


class UnitOfWork(ABC):
    users: UserRepository
    plans: PlanRepository

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
