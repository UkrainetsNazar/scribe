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
    AppError,
    InvalidMediaError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
    UploadMissingError,
)
from scribe.domain.pipeline import ProcessingStage
from scribe.domain.quota import ensure_within_quota, usage_period
from scribe.domain.recordings import MAX_DURATION_SEC, Recording, RecordingStatus
from scribe.domain.users import User


@dataclass(frozen=True, slots=True)
class CreatedRecording:
    recording: Recording
    upload_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RecordingView:
    recording: Recording
    stages: list[ProcessingStage]


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
    """Validates the uploaded file and moves the recording to `processing`.

    Missing file: 422 and the state stays `created`, so the client can upload and retry.
    Invalid file, too long, or quota exceeded: the recording becomes `failed` and the call is 422.
    Repeated call for a recording that is already processing: returns it unchanged.
    The minutes are charged once, when processing starts; retry does not charge again.
    """

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
            # Row lock: concurrent calls are serialized, the second one sees `processing`
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
            user = await uow.users.get_by_id(owner_id)
            if user is None:
                raise NotFoundError("User not found")

            recording.mark_uploaded()
            if stored.size != recording.size_bytes:
                raise await self._fail(
                    uow,
                    recording,
                    InvalidMediaError("Uploaded file size does not match the declared size"),
                )

            read_url = await self._storage.create_internal_read_url(
                self._bucket, recording.storage_key, self.READ_URL_TTL_SECONDS
            )
            try:
                media = await self._probe.probe(read_url)
            except InvalidMediaError as exc:
                raise await self._fail(uow, recording, exc) from exc
            if media.duration_sec > MAX_DURATION_SEC:
                raise await self._fail(
                    uow, recording, InvalidMediaError("Recording is longer than 4 hours")
                )

            # The usage row is locked, so concurrent uploads of one user cannot overshoot the quota
            period = usage_period(datetime.now(UTC))
            used = await uow.usage.lock_minutes(owner_id, period)
            try:
                minutes = ensure_within_quota(user.plan, used, media.duration_sec)
            except QuotaExceededError as exc:
                raise await self._fail(uow, recording, exc) from exc

            recording.start_processing(media.duration_sec)
            await uow.usage.add_minutes(owner_id, period, minutes)
            await uow.stages.reset_incomplete(recording.id)
            await uow.recordings.save(recording)
            uow.add_events(recording.pull_events())
            await uow.commit()
        return recording

    @staticmethod
    async def _fail(uow: UnitOfWork, recording: Recording, error: AppError) -> AppError:
        """Persists the failure (the commit must happen before the error is raised)."""
        recording.fail(error.message)
        await uow.recordings.save(recording)
        uow.add_events(recording.pull_events())
        await uow.commit()
        return error


class RetryRecording:
    """failed/cancelled -> processing. Completed stages are kept, the rest run again."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, recording_id: UUID) -> Recording:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
            if recording is None:
                raise NotFoundError("Recording not found")
            if recording.status is RecordingStatus.PROCESSING:
                return recording  # idempotent: already running
            recording.retry()  # 409 unless failed or cancelled
            await uow.stages.reset_incomplete(recording.id)
            await uow.recordings.save(recording)
            uow.add_events(recording.pull_events())
            await uow.commit()
        return recording


class CancelRecording:
    """processing -> cancelled. Running stages notice it at their next progress report."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, recording_id: UUID) -> Recording:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
            if recording is None:
                raise NotFoundError("Recording not found")
            if recording.status is RecordingStatus.CANCELLED:
                return recording  # idempotent
            recording.cancel()  # 409 unless processing
            await uow.recordings.save(recording)
            uow.add_events(recording.pull_events())
            await uow.commit()
        return recording


class GetRecording:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, recording_id: UUID) -> RecordingView:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id)
            if recording is None:
                raise NotFoundError("Recording not found")
            stages = await uow.stages.list_for_recording(recording.id)
        return RecordingView(recording=recording, stages=stages)


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
    """Marks the recording as deleted. Files and Mongo data are removed on the
    RecordingDeleted event."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, recording_id: UUID) -> None:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
            if recording is None:
                raise NotFoundError("Recording not found")
            recording.delete()  # 409 for states that do not allow it
            await uow.recordings.save(recording)
            uow.add_events(recording.pull_events())
            await uow.commit()
