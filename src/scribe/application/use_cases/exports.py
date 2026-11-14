from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import orjson

from scribe.application.interfaces import ObjectStorage, UnitOfWorkFactory
from scribe.application.use_cases.results import require_completed
from scribe.domain.errors import NotFoundError
from scribe.domain.exports import (
    CONTENT_TYPES,
    Export,
    ExportFormat,
    export_filename,
    export_key,
    render_export,
)
from scribe.domain.pipeline import SEGMENTS, artifact_key
from scribe.domain.transcript import Transcription


class CreateExport:
    """Builds the file from the transcript artifact and stores it in the exports bucket.

    Idempotent: repeating the call rebuilds the same file under the same key."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        storage: ObjectStorage,
        artifacts_bucket: str,
        exports_bucket: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._artifacts = artifacts_bucket
        self._exports = exports_bucket

    async def execute(self, owner_id: UUID, recording_id: UUID, fmt: ExportFormat) -> Export:
        recording = await require_completed(self._uow_factory, owner_id, recording_id)
        raw = await self._storage.get_bytes(
            self._artifacts, artifact_key(owner_id, recording.id, SEGMENTS)
        )
        if raw is None:
            raise NotFoundError("Transcript is not available")
        content = render_export(
            fmt,
            Transcription.from_dict(orjson.loads(raw)),
            recording_id=recording.id,
            title=recording.title,
            duration_sec=recording.duration_sec,
        )
        key = export_key(owner_id, recording.id, fmt)
        await self._storage.put_bytes(self._exports, key, content, CONTENT_TYPES[fmt])

        async with self._uow_factory() as uow:
            await uow.exports.upsert(Export.create(recording.id, fmt, key))
            export = await uow.exports.get(recording.id, fmt)
            await uow.commit()
        if export is None:
            raise RuntimeError("Export row disappeared after upsert")
        return export


@dataclass(frozen=True, slots=True)
class ExportLink:
    format: ExportFormat
    url: str
    expires_at: datetime


class GetExportLink:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, storage: ObjectStorage, bucket: str, ttl_min: int
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._bucket = bucket
        self._ttl_seconds = ttl_min * 60

    async def execute(self, owner_id: UUID, recording_id: UUID, fmt: ExportFormat) -> ExportLink:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id)
            if recording is None:
                raise NotFoundError("Recording not found")
            export = await uow.exports.get(recording_id, fmt)
        if export is None:
            raise NotFoundError("Export has not been created yet")
        url = await self._storage.create_download_url(
            self._bucket,
            export.storage_key,
            self._ttl_seconds,
            export_filename(recording.title, fmt),
        )
        expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl_seconds)
        return ExportLink(format=fmt, url=url, expires_at=expires_at)
