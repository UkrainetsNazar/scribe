from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pymongo import AsyncMongoClient, ReplaceOne
from pymongo.asynchronous.database import AsyncDatabase

from scribe.application.pipeline_ports import TranscriptIndex
from scribe.domain.transcript import Summary, Transcription

BATCH_SIZE = 1000


def create_mongo_client(url: str) -> AsyncMongoClient[dict[str, Any]]:
    return AsyncMongoClient(url, tz_aware=True)


def get_database(
    client: AsyncMongoClient[dict[str, Any]], name: str
) -> AsyncDatabase[dict[str, Any]]:
    return client[name]


class MongoTranscriptIndex(TranscriptIndex):
    """Segments are separate documents, so long recordings never hit the 16 MB limit.
    Ids are stored as strings to avoid BSON UUID representation issues."""

    def __init__(self, db: AsyncDatabase[dict[str, Any]]) -> None:
        self._segments = db["transcript_segments"]
        self._transcripts = db["transcripts"]
        self._summaries = db["summaries"]

    async def _ensure_indexes(self) -> None:
        await self._segments.create_index(
            [("recording_id", 1), ("idx", 1)], unique=True, name="recording_idx"
        )
        # `none` disables stemming, so the index works for any language (incl. Ukrainian)
        await self._segments.create_index(
            [("text", "text")],
            default_language="none",
            language_override="_lang",
            name="text_search",
        )
        await self._segments.create_index([("owner_id", 1)], name="owner")
        await self._transcripts.create_index("recording_id", unique=True)
        await self._summaries.create_index("recording_id", unique=True)

    async def index(
        self,
        *,
        recording_id: UUID,
        owner_id: UUID,
        transcription: Transcription,
        summary: Summary | None,
    ) -> None:
        await self._ensure_indexes()
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
