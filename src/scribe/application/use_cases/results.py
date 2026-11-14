from dataclasses import dataclass
from uuid import UUID

from scribe.application.interfaces import UnitOfWorkFactory
from scribe.application.pagination import decode_index_cursor, encode_index_cursor
from scribe.application.read_ports import IndexedSegment, TranscriptReader
from scribe.domain.errors import DomainValidationError, InvalidStateError, NotFoundError
from scribe.domain.recordings import Recording, RecordingStatus
from scribe.domain.search import make_snippet
from scribe.domain.transcript import Segment, Summary

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 200
# A search covers at most this many of the user's newest completed recordings
SEARCH_MAX_RECORDINGS = 5000


async def require_completed(
    uow_factory: UnitOfWorkFactory, owner_id: UUID, recording_id: UUID
) -> Recording:
    """404 for unknown or foreign recordings, 409 while the results are not ready."""
    async with uow_factory() as uow:
        recording = await uow.recordings.get(recording_id, owner_id)
    if recording is None:
        raise NotFoundError("Recording not found")
    if recording.status is not RecordingStatus.COMPLETED:
        raise InvalidStateError("Results are not ready: processing has not completed")
    return recording


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    language: str
    items: list[IndexedSegment]
    next_cursor: str | None


class GetTranscript:
    def __init__(self, uow_factory: UnitOfWorkFactory, reader: TranscriptReader) -> None:
        self._uow_factory = uow_factory
        self._reader = reader

    async def execute(
        self,
        owner_id: UUID,
        recording_id: UUID,
        start_from: float,
        limit: int,
        cursor: str | None,
    ) -> TranscriptPage:
        after_idx = decode_index_cursor(cursor) if cursor else -1
        recording = await require_completed(self._uow_factory, owner_id, recording_id)
        rows = await self._reader.list_segments(
            recording.id, owner_id, from_sec=start_from, after_idx=after_idx, limit=limit
        )
        if len(rows) > limit:
            items = rows[:limit]
            next_cursor: str | None = encode_index_cursor(items[-1].idx)
        else:
            items, next_cursor = rows, None
        return TranscriptPage(language=recording.language, items=items, next_cursor=next_cursor)


class GetSummary:
    def __init__(self, uow_factory: UnitOfWorkFactory, reader: TranscriptReader) -> None:
        self._uow_factory = uow_factory
        self._reader = reader

    async def execute(self, owner_id: UUID, recording_id: UUID) -> Summary:
        recording = await require_completed(self._uow_factory, owner_id, recording_id)
        summary = await self._reader.get_summary(recording.id)
        if summary is None:
            raise NotFoundError("Summary not found")
        return summary


@dataclass(frozen=True, slots=True)
class SearchResult:
    recording_id: UUID
    idx: int
    segment: Segment
    snippet: str


class SearchTranscripts:
    def __init__(self, uow_factory: UnitOfWorkFactory, reader: TranscriptReader) -> None:
        self._uow_factory = uow_factory
        self._reader = reader

    async def execute(
        self, owner_id: UUID, query: str, project_id: UUID | None, limit: int
    ) -> list[SearchResult]:
        query = " ".join(query.split())
        if not MIN_QUERY_LENGTH <= len(query) <= MAX_QUERY_LENGTH:
            raise DomainValidationError(
                f"Query must be {MIN_QUERY_LENGTH}-{MAX_QUERY_LENGTH} characters"
            )
        async with self._uow_factory() as uow:
            if project_id is not None and await uow.projects.get(project_id, owner_id) is None:
                raise NotFoundError("Project not found")
            recording_ids = await uow.recordings.list_completed_ids(
                owner_id, project_id, SEARCH_MAX_RECORDINGS
            )
        hits = await self._reader.search(owner_id, query, recording_ids, limit)
        return [
            SearchResult(
                recording_id=hit.recording_id,
                idx=hit.idx,
                segment=hit.segment,
                snippet=make_snippet(hit.segment.text, query),
            )
            for hit in hits
        ]
