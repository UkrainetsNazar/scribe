from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pymongo import AsyncMongoClient, ReplaceOne
from pymongo.asynchronous.database import AsyncDatabase

from scribe.application.pipeline_ports import TranscriptIndex
from scribe.application.read_ports import IndexedSegment, SearchHit, TranscriptReader
from scribe.domain.transcript import Segment, Summary, Transcription

BATCH_SIZE = 1000

Db = AsyncDatabase[dict[str, Any]]


def create_mongo_client(url: str) -> AsyncMongoClient[dict[str, Any]]:
    return AsyncMongoClient(url, tz_aware=True)


def get_database(client: AsyncMongoClient[dict[str, Any]], name: str) -> Db:
    return client[name]


async def ensure_indexes(db: Db) -> None:
    """Idempotent. Segments are separate documents, so long recordings never hit the 16 MB limit."""
    segments = db["transcript_segments"]
    await segments.create_index(
        [("recording_id", 1), ("idx", 1)], unique=True, name="recording_idx"
    )
    # `none` disables stemming, so the index works for any language (incl. Ukrainian)
    await segments.create_index(
        [("text", "text")],
        default_language="none",
        language_override="_lang",
        name="text_search",
    )
    await segments.create_index([("owner_id", 1)], name="owner")
    await db["transcripts"].create_index("recording_id", unique=True)
    await db["summaries"].create_index("recording_id", unique=True)


class MongoTranscriptIndex(TranscriptIndex):
    """Write side. Ids are stored as strings to avoid BSON UUID representation issues."""

    def __init__(self, db: Db) -> None:
        self._db = db
        self._segments = db["transcript_segments"]
        self._transcripts = db["transcripts"]
        self._summaries = db["summaries"]

    async def index(
        self,
        *,
        recording_id: UUID,
        owner_id: UUID,
        transcription: Transcription,
        summary: Summary | None,
    ) -> None:
        await ensure_indexes(self._db)
        rid, oid = str(recording_id), str(owner_id)
        now = datetime.now(UTC)

        operations = [
            ReplaceOne(
                {"recording_id": rid, "idx": i},
                {"recording_id": rid, "owner_id": oid, "idx": i, **segment.to_dict()},
                upsert=True,
            )
            for i, segment in enumerate(transcription.segments)
        ]
        for start in range(0, len(operations), BATCH_SIZE):
            await self._segments.bulk_write(operations[start : start + BATCH_SIZE], ordered=False)
        # A rerun may produce fewer segments than an earlier attempt
        await self._segments.delete_many(
            {"recording_id": rid, "idx": {"$gte": len(transcription.segments)}}
        )

        await self._transcripts.replace_one(
            {"recording_id": rid},
            {
                "recording_id": rid,
                "language": transcription.language,
                "model": transcription.model,
                "segment_count": len(transcription.segments),
                "created_at": now,
            },
            upsert=True,
        )
        if summary is not None:
            await self._summaries.replace_one(
                {"recording_id": rid},
                {"recording_id": rid, **summary.to_dict(), "created_at": now},
                upsert=True,
            )

    async def remove(self, recording_id: UUID) -> None:
        rid = str(recording_id)
        await self._segments.delete_many({"recording_id": rid})
        await self._transcripts.delete_many({"recording_id": rid})
        await self._summaries.delete_many({"recording_id": rid})


class MongoTranscriptReader(TranscriptReader):
    def __init__(self, db: Db) -> None:
        self._db = db
        self._segments = db["transcript_segments"]
        self._summaries = db["summaries"]
        self._indexes_ready = False

    async def _ensure_indexes(self) -> None:
        # $text fails without a text index, which exists only after the first recording is indexed
        if not self._indexes_ready:
            await ensure_indexes(self._db)
            self._indexes_ready = True

    async def list_segments(
        self, recording_id: UUID, owner_id: UUID, *, from_sec: float, after_idx: int, limit: int
    ) -> list[IndexedSegment]:
        cursor = (
            self._segments.find(
                {
                    "recording_id": str(recording_id),
                    "owner_id": str(owner_id),
                    "idx": {"$gt": after_idx},
                    "end": {"$gte": from_sec},
                }
            )
            .sort("idx", 1)
            .limit(limit + 1)
        )
        return [
            IndexedSegment(idx=int(doc["idx"]), segment=Segment.from_dict(doc))
            for doc in await cursor.to_list()
        ]

    async def get_summary(self, recording_id: UUID) -> Summary | None:
        doc = await self._summaries.find_one({"recording_id": str(recording_id)})
        return Summary.from_dict(doc) if doc else None

    async def search(self, owner_id: UUID, query: str, *, skip: int, limit: int) -> list[SearchHit]:
        await self._ensure_indexes()
        cursor = (
            self._segments.find(
                {"$text": {"$search": query}, "owner_id": str(owner_id)},
                {"score": {"$meta": "textScore"}},
            )
            # _id breaks ties, so paging with skip is stable
            .sort([("score", {"$meta": "textScore"}), ("_id", 1)])
            .skip(skip)
            .limit(limit)
        )
        return [
            SearchHit(
                recording_id=UUID(doc["recording_id"]),
                idx=int(doc["idx"]),
                segment=Segment.from_dict(doc),
                score=float(doc["score"]),
            )
            for doc in await cursor.to_list()
        ]
