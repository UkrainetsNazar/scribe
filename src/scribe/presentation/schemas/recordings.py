from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

from scribe.application.use_cases.recordings import CreatedRecording, RecordingView
from scribe.domain.pipeline import ProcessingStage, StageName, StageStatus
from scribe.domain.recordings import Recording, RecordingStatus

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class RecordingCreateRequest(BaseModel):
    project_id: UUID
    title: Title
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(max_length=100)
    size_bytes: int = Field(ge=1, le=2_147_483_648)
    language: str = Field(default="auto", max_length=10)


class StageResponse(BaseModel):
    name: StageName
    status: StageStatus
    progress: int = Field(ge=0, le=100)
    error: str | None = None

    @classmethod
    def from_domain(cls, stage: ProcessingStage) -> Self:
        return cls(name=stage.name, status=stage.status, progress=stage.progress, error=stage.error)


class RecordingResponse(BaseModel):
    id: UUID
    project_id: UUID
    title: str
    status: RecordingStatus
    duration_sec: float | None
    language: str | None
    stages: list[StageResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, recording: Recording, stages: Sequence[ProcessingStage] = ()) -> Self:
        return cls(
            id=recording.id,
            project_id=recording.project_id,
            title=recording.title,
            status=recording.status,
            duration_sec=recording.duration_sec,
            language=recording.language,
            stages=[StageResponse.from_domain(s) for s in stages],
            created_at=recording.created_at,
            updated_at=recording.updated_at,
        )

    @classmethod
    def from_view(cls, view: RecordingView) -> Self:
        return cls.from_domain(view.recording, view.stages)


class RecordingCreatedResponse(BaseModel):
    recording: RecordingResponse
    upload_url: str
    expires_at: datetime

    @classmethod
    def from_domain(cls, created: CreatedRecording) -> Self:
        return cls(
            recording=RecordingResponse.from_domain(created.recording),
            upload_url=created.upload_url,
            expires_at=created.expires_at,
        )


class RecordingPageResponse(BaseModel):
    items: list[RecordingResponse]
    next_cursor: str | None
