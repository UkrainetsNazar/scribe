from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from scribe.domain.transcript import Segment, Summary


@dataclass(frozen=True, slots=True)
class IndexedSegment:
    idx: int
    segment: Segment


@dataclass(frozen=True, slots=True)
class SearchHit:
    recording_id: UUID
    idx: int
    segment: Segment
    score: float


class TranscriptReader(ABC):
    """Read side of the transcript store. Ownership is checked by the callers (PostgreSQL)."""

    @abstractmethod
    async def list_segments(
        self, recording_id: UUID, owner_id: UUID, *, from_sec: float, after_idx: int, limit: int
    ) -> list[IndexedSegment]:
        """Segments with `idx > after_idx` that end at or after `from_sec`, ordered by idx.
        Returns up to limit + 1 rows: the extra one signals a next page."""

    @abstractmethod
    async def get_summary(self, recording_id: UUID) -> Summary | None: ...

    @abstractmethod
    async def search(
        self, owner_id: UUID, query: str, recording_ids: Sequence[UUID], limit: int
    ) -> list[SearchHit]:
        """Best matches first. Only the given recordings are searched."""
