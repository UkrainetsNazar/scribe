from typing import Annotated
from uuid import UUID

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Query, status

from scribe.application.use_cases.exports import CreateExport, GetExportLink
from scribe.application.use_cases.results import GetSummary, GetTranscript, SearchTranscripts
from scribe.container import Container
from scribe.domain.exports import ExportFormat
from scribe.domain.users import User
from scribe.presentation.deps import get_current_user
from scribe.presentation.schemas.results import (
    ExportLinkResponse,
    ExportRequest,
    ExportResponse,
    SearchHitResponse,
    SearchResponse,
    SummaryResponse,
    TranscriptPageResponse,
)

router = APIRouter(tags=["results"])


@router.get("/recordings/{recording_id}/transcript")
@inject
async def get_transcript(
    recording_id: UUID,
    start_from: Annotated[float, Query(alias="from", ge=0)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    user: User = Depends(get_current_user),
    use_case: GetTranscript = Depends(Provide[Container.get_transcript]),
) -> TranscriptPageResponse:
    page = await use_case.execute(user.id, recording_id, start_from, limit, cursor)
    return TranscriptPageResponse.from_page(page)


@router.get("/recordings/{recording_id}/summary")
@inject
async def get_summary(
    recording_id: UUID,
    user: User = Depends(get_current_user),
    use_case: GetSummary = Depends(Provide[Container.get_summary]),
) -> SummaryResponse:
    return SummaryResponse.from_domain(await use_case.execute(user.id, recording_id))


@router.post("/recordings/{recording_id}/exports", status_code=status.HTTP_202_ACCEPTED)
@inject
async def create_export(
    recording_id: UUID,
    body: ExportRequest,
    user: User = Depends(get_current_user),
    use_case: CreateExport = Depends(Provide[Container.create_export]),
) -> ExportResponse:
    export = await use_case.execute(user.id, recording_id, body.format)
    return ExportResponse.from_domain(export)


@router.get("/recordings/{recording_id}/exports/{export_format}")
@inject
async def get_export(
    recording_id: UUID,
    export_format: ExportFormat,
    user: User = Depends(get_current_user),
    use_case: GetExportLink = Depends(Provide[Container.get_export_link]),
) -> ExportLinkResponse:
    link = await use_case.execute(user.id, recording_id, export_format)
    return ExportLinkResponse.from_domain(link)


@router.get("/search")
@inject
async def search(
    q: Annotated[str, Query(min_length=2, max_length=200)],
    project_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    user: User = Depends(get_current_user),
    use_case: SearchTranscripts = Depends(Provide[Container.search_transcripts]),
) -> SearchResponse:
    results = await use_case.execute(user.id, q, project_id, limit)
    return SearchResponse(items=[SearchHitResponse.from_domain(r) for r in results])
