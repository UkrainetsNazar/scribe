from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel

from scribe.application.read_ports import IndexedSegment
from scribe.application.use_cases.exports import ExportLink
from scribe.application.use_cases.results import SearchResult, TranscriptPage
from scribe.domain.exports import Export, ExportFormat, ExportStatus
from scribe.domain.transcript import Summary


class SegmentResponse(BaseModel):
    idx: int
    start: float
    end: float
    text: str
    speaker: str | None
    confidence: float

    @classmethod
    def from_indexed(cls, item: IndexedSegment) -> Self:
        return cls(
            idx=item.idx,
            start=item.segment.start,
            end=item.segment.end,
            text=item.segment.text,
            speaker=item.segment.speaker,
            confidence=item.segment.confidence,
        )


class TranscriptPageResponse(BaseModel):
    language: str
    items: list[SegmentResponse]
    next_cursor: str | None

    @classmethod
    def from_page(cls, page: TranscriptPage) -> Self:
        return cls(
            language=page.language,
            items=[SegmentResponse.from_indexed(i) for i in page.items],
            next_cursor=page.next_cursor,
        )


class SummaryResponse(BaseModel):
    summary: str
    topics: list[str]
    action_items: list[str]
    model: str

    @classmethod
    def from_domain(cls, summary: Summary) -> Self:
        return cls(
            summary=summary.summary,
            topics=summary.topics,
            action_items=summary.action_items,
            model=summary.model,
        )


class ExportRequest(BaseModel):
    format: ExportFormat


class ExportResponse(BaseModel):
    format: ExportFormat
    status: ExportStatus

    @classmethod
    def from_domain(cls, export: Export) -> Self:
        return cls(format=export.format, status=export.status)


class ExportLinkResponse(BaseModel):
    format: ExportFormat
    url: str
    expires_at: datetime

    @classmethod
    def from_domain(cls, link: ExportLink) -> Self:
        return cls(format=link.format, url=link.url, expires_at=link.expires_at)


class SearchHitResponse(BaseModel):
    recording_id: UUID
    segment: SegmentResponse
    snippet: str

    @classmethod
    def from_domain(cls, result: SearchResult) -> Self:
        return cls(
            recording_id=result.recording_id,
            segment=SegmentResponse.from_indexed(IndexedSegment(result.idx, result.segment)),
            snippet=result.snippet,
        )


class SearchResponse(BaseModel):
    items: list[SearchHitResponse]
