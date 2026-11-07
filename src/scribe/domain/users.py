from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self
from uuid import UUID, uuid4


def normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclass(frozen=True, slots=True)
class Plan:
    id: int
    name: str
    monthly_minutes: int
    max_file_mb: int


@dataclass(slots=True)
class User:
    id: UUID
    email: str
    password_hash: str
    plan: Plan
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def register(cls, email: str, password_hash: str, plan: Plan) -> Self:
        return cls(
            id=uuid4(),
            email=normalize_email(email),
            password_hash=password_hash,
            plan=plan,
        )
