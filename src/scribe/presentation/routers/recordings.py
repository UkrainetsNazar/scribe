from typing import Annotated
from uuid import UUID

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Query, Response, status

from scribe.application.use_cases.recordings import (
    CancelRecording,
    CompleteUpload,
    CreateRecording,
    DeleteRecording,
    GetRecording,
    ListRecordings,
    RetryRecording,
)
from scribe.container import Container
from scribe.domain.recordings import RecordingStatus
from scribe.domain.users import User
from scribe.presentation.deps import get_current_user
from scribe.presentation.schemas.recordings import (
    RecordingCreatedResponse,
    RecordingCreateRequest,
    RecordingPageResponse,
    RecordingResponse,
)

router = APIRouter(prefix="/recordings", tags=["recordings"])


@router.post("", status_code=status.HTTP_201_CREATED)
@inject
async def create_recording(
    body: RecordingCreateRequest,
    user: User = Depends(get_current_user),
    use_case: CreateRecording = Depends(Provide[Container.create_recording]),
) -> RecordingCreatedResponse:
    created = await use_case.execute(
        user,
        body.project_id,
        body.title,
        body.filename,
        body.content_type,
        body.size_bytes,
        body.language,
    )
    return RecordingCreatedResponse.from_domain(created)


@router.get("")
@inject
async def list_recordings(
    project_id: UUID | None = None,
    status_filter: Annotated[RecordingStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    user: User = Depends(get_current_user),
    use_case: ListRecordings = Depends(Provide[Container.list_recordings]),
) -> RecordingPageResponse:
    page = await use_case.execute(user.id, project_id, status_filter, limit, cursor)
    return RecordingPageResponse(
        items=[RecordingResponse.from_domain(r) for r in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{recording_id}")
@inject
async def get_recording(
    recording_id: UUID,
    user: User = Depends(get_current_user),
    use_case: GetRecording = Depends(Provide[Container.get_recording]),
) -> RecordingResponse:
    return RecordingResponse.from_view(await use_case.execute(user.id, recording_id))


@router.post("/{recording_id}/complete-upload", status_code=status.HTTP_202_ACCEPTED)
@inject
async def complete_upload(
    recording_id: UUID,
    user: User = Depends(get_current_user),
    use_case: CompleteUpload = Depends(Provide[Container.complete_upload]),
    get_use_case: GetRecording = Depends(Provide[Container.get_recording]),
) -> RecordingResponse:
    await use_case.execute(user.id, recording_id)
    return RecordingResponse.from_view(await get_use_case.execute(user.id, recording_id))


@router.post("/{recording_id}/retry", status_code=status.HTTP_202_ACCEPTED)
@inject
async def retry_recording(
    recording_id: UUID,
    user: User = Depends(get_current_user),
    use_case: RetryRecording = Depends(Provide[Container.retry_recording]),
    get_use_case: GetRecording = Depends(Provide[Container.get_recording]),
) -> RecordingResponse:
    await use_case.execute(user.id, recording_id)
    return RecordingResponse.from_view(await get_use_case.execute(user.id, recording_id))


@router.post("/{recording_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
@inject
async def cancel_recording(
    recording_id: UUID,
    user: User = Depends(get_current_user),
    use_case: CancelRecording = Depends(Provide[Container.cancel_recording]),
    get_use_case: GetRecording = Depends(Provide[Container.get_recording]),
) -> RecordingResponse:
    await use_case.execute(user.id, recording_id)
    return RecordingResponse.from_view(await get_use_case.execute(user.id, recording_id))


@router.delete("/{recording_id}", status_code=status.HTTP_204_NO_CONTENT)
@inject
async def delete_recording(
    recording_id: UUID,
    user: User = Depends(get_current_user),
    use_case: DeleteRecording = Depends(Provide[Container.delete_recording]),
) -> Response:
    await use_case.execute(user.id, recording_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
