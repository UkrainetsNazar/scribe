from datetime import datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator

from scribe.domain.projects import Project

ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class ProjectCreateRequest(BaseModel):
    name: ProjectName
    description: str | None = Field(default=None, max_length=1000)


class ProjectUpdateRequest(BaseModel):
    name: ProjectName | None = None
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("name")
    @classmethod
    def name_is_not_null(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("name cannot be null")
        return v


class ProjectResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    created_at: datetime

    @classmethod
    def from_domain(cls, project: Project) -> Self:
        return cls(
            id=project.id,
            name=project.name,
            description=project.description,
            created_at=project.created_at,
        )


class ProjectPageResponse(BaseModel):
    items: list[ProjectResponse]
    next_cursor: str | None
