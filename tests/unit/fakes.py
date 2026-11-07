from types import TracebackType
from typing import Self
from uuid import UUID

from scribe.application.interfaces import (
    PasswordHasher,
    PlanRepository,
    RefreshTokenStore,
    UnitOfWork,
    UserRepository,
)
from scribe.domain.errors import EmailTakenError
from scribe.domain.users import Plan, User


class InMemoryUserRepository(UserRepository):
    def __init__(self, users: dict[UUID, User]) -> None:
        self._users = users

    async def add(self, user: User) -> None:
        if any(u.email == user.email for u in self._users.values()):
            raise EmailTakenError
        self._users[user.id] = user

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._users.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self._users.values() if u.email == email), None)


class InMemoryPlanRepository(PlanRepository):
    def __init__(self, plans: dict[str, Plan]) -> None:
        self._plans = plans

    async def get_by_name(self, name: str) -> Plan | None:
        return self._plans.get(name)


class FakeUnitOfWork(UnitOfWork):
    def __init__(self, users: dict[UUID, User], plans: dict[str, Plan]) -> None:
        self.users = InMemoryUserRepository(users)
        self.plans = InMemoryPlanRepository(plans)
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


class FakePasswordHasher(PasswordHasher):
    @property
    def dummy_hash(self) -> str:
        return "hashed::dummy"

    async def hash(self, password: str) -> str:
        return f"hashed::{password}"

    async def verify(self, password: str, hashed: str) -> bool:
        return hashed == f"hashed::{password}"


class InMemoryRefreshTokenStore(RefreshTokenStore):
    def __init__(self) -> None:
        self.items: dict[str, UUID] = {}

    async def save(self, jti: str, user_id: UUID, ttl_seconds: int) -> None:
        self.items[jti] = user_id

    async def consume(self, jti: str) -> UUID | None:
        return self.items.pop(jti, None)

    async def revoke(self, jti: str) -> None:
        self.items.pop(jti, None)
