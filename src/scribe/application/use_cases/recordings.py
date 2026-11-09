from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from scribe.application.interfaces import (
    MediaProbe,
    ObjectStorage,
    UnitOfWork,
    UnitOfWorkFactory,
)
from scribe.application.pagination import Cursor, Page, build_page, decode_cursor
from scribe.domain.errors import (
    InvalidMediaError,
    InvalidStateError,
    NotFoundError,
    UploadMissingError,
)
from scribe.domain.recordings import MAX_DURATION_SEC, Recording, RecordingStatus
from scribe.domain.users import User


@dataclass(frozen=True, slots=True)
class CreatedRecording:
    recording: Recording
    upload_url: str
    expires_at: datetime


class CreateRecording:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        storage: ObjectStorage,
        bucket: str,
        upload_ttl_min: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._bucket = bucket
        self._ttl_seconds = upload_ttl_min * 60

    async def execute(
        self,
        user: User,
        project_id: UUID,
        title: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        language: str,
    ) -> CreatedRecording:
        async with self._uow_factory() as uow:
            if await uow.projects.get(project_id, user.id) is None:
                raise NotFoundError("Project not found")
            recording = Recording.create(
                project_id=project_id,
                owner_id=user.id,
                title=title,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                language=language,
                plan=user.plan,
            )
            await uow.recordings.add(recording)
            uow.add_events(recording.pull_events())
            await uow.commit()

        url = await self._storage.create_upload_url(
            self._bucket, recording.storage_key, self._ttl_seconds
        )
        expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl_seconds)
        return CreatedRecording(recording=recording, upload_url=url, expires_at=expires_at)


class CompleteUpload:
    READ_URL_TTL_SECONDS = 300

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        storage: ObjectStorage,
        probe: MediaProbe,
        bucket: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._probe = probe
        self._bucket = bucket

    async def execute(self, owner_id: UUID, recording_id: UUID) -> Recording:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
            if recording is None:
                raise NotFoundError("Recording not found")
            if recording.status in (RecordingStatus.PROCESSING, RecordingStatus.COMPLETED):
                return recording
            if recording.status is not RecordingStatus.CREATED:
                raise InvalidStateError(f"Cannot complete upload in state '{recording.status}'")

            stored = await self._storage.stat(self._bucket, recording.storage_key)
            if stored is None:
                raise UploadMissingError

            recording.mark_uploaded()
            if stored.size != recording.size_bytes:
                raise await self._fail(
                    uow, recording, "Uploaded file size does not match the declared size"
                )

            read_url = await self._storage.create_internal_read_url(
                self._bucket, recording.storage_key, self.READ_URL_TTL_SECONDS
            )
            try:
                media = await self._probe.probe(read_url)
            except InvalidMediaError as exc:
                raise await self._fail(uow, recording, exc.message) from exc
            if media.duration_sec > MAX_DURATION_SEC:
                raise await self._fail(uow, recording, "Recording is longer than 4 hours")

            recording.start_processing(media.duration_sec)
            await uow.recordings.save(recording)
            uow.add_events(recording.pull_events())
            await uow.commit()
        return recording

    @staticmethod
    async def _fail(uow: UnitOfWork, recording: Recording, reason: str) -> InvalidMediaError:
        recording.fail(reason)
        await uow.recordings.save(recording)
        uow.add_events(recording.pull_events())
        await uow.commit()
        return InvalidMediaError(reason)


class GetRecording:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, recording_id: UUID) -> Recording:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id)
        if recording is None:
            raise NotFoundError("Recording not found")
        return recording


class ListRecordings:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self,
        owner_id: UUID,
        project_id: UUID | None,
        status: RecordingStatus | None,
        limit: int,
        cursor: str | None,
    ) -> Page[Recording]:
        position = decode_cursor(cursor) if cursor else None
        async with self._uow_factory() as uow:
            rows = await uow.recordings.list_by_owner(
                owner_id, project_id=project_id, status=status, limit=limit, cursor=position
            )
        return build_page(rows, limit, lambda r: Cursor(r.created_at, r.id))


class DeleteRecording:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, recording_id: UUID) -> None:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
            if recording is None:
                raise NotFoundError("Recording not found")
            recording.delete()
            await uow.recordings.save(recording)
            uow.add_events(recording.pull_events())
            await uow.commit()
