from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self
from uuid import UUID, uuid4

from scribe.domain.errors import DomainValidationError

MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 1000


def _clean_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise DomainValidationError(f"Project name must be 1-{MAX_NAME_LENGTH} characters")
    return name


def _clean_description(description: str | None) -> str | None:
    if description is not None and len(description) > MAX_DESCRIPTION_LENGTH:
        raise DomainValidationError(
            f"Description must be at most {MAX_DESCRIPTION_LENGTH} characters"
        )
    return description


@dataclass(slots=True)
class Project:
    id: UUID
    owner_id: UUID
    name: str
    description: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None

    @classmethod
    def create(cls, owner_id: UUID, name: str, description: str | None = None) -> Self:
        return cls(
            id=uuid4(),
            owner_id=owner_id,
            name=_clean_name(name),
            description=_clean_description(description),
        )

    def update(self, changes: Mapping[str, str | None]) -> None:
        if "name" in changes:
            name = changes["name"]
            if name is None:
                raise DomainValidationError("Project name cannot be null")
            self.name = _clean_name(name)
        if "description" in changes:
            self.description = _clean_description(changes["description"])

    def delete(self) -> None:
        self.deleted_at = datetime.now(UTC)
