from datetime import UTC, datetime

import pytest

from scribe.application.interfaces import UnitOfWorkFactory
from scribe.application.use_cases.recordings import (
    CancelRecording,
    CompleteUpload,
    CreatedRecording,
    CreateRecording,
    DeleteRecording,
    GetRecording,
    RetryRecording,
)
from scribe.domain.errors import (
    FileTooLargeError,
    InvalidMediaError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
    UploadMissingError,
)
from scribe.domain.events import RecordingCreated, RecordingProcessingRequested
from scribe.domain.pipeline import PIPELINE_STAGES, SOURCE_COPY, artifact_key
from scribe.domain.projects import Project
from scribe.domain.quota import usage_period
from scribe.domain.recordings import MAX_DURATION_SEC, Recording, RecordingStatus
from scribe.domain.users import User
from tests.unit.fakes import FREE_PLAN, FakeDb, FakeMediaProbe, FakeObjectStorage

BUCKET = "uploads"


@pytest.fixture
def user(db: FakeDb) -> User:
    user = User.register("a@example.com", "hash", FREE_PLAN)
    db.users[user.id] = user
    return user


@pytest.fixture
def project(db: FakeDb, user: User) -> Project:
    project = Project.create(user.id, "Demo")
    db.projects[project.id] = project
    return project


@pytest.fixture
def storage() -> FakeObjectStorage:
    return FakeObjectStorage()


@pytest.fixture
def probe() -> FakeMediaProbe:
    return FakeMediaProbe()


@pytest.fixture
def create(uow_factory: UnitOfWorkFactory, storage: FakeObjectStorage) -> CreateRecording:
    return CreateRecording(uow_factory, storage, BUCKET, upload_ttl_min=15)


@pytest.fixture
def complete(
    uow_factory: UnitOfWorkFactory, storage: FakeObjectStorage, probe: FakeMediaProbe
) -> CompleteUpload:
    return CompleteUpload(uow_factory, storage, probe, BUCKET, "artifacts")


async def _create(
    create: CreateRecording, user: User, project: Project, size: int = 1024
) -> CreatedRecording:
    return await create.execute(user, project.id, "Talk", "talk.wav", "audio/wav", size, "auto")


async def _uploaded(
    create: CreateRecording,
    storage: FakeObjectStorage,
    user: User,
    project: Project,
    size: int = 1024,
    stored_size: int | None = None,
) -> Recording:
    recording = (await _create(create, user, project, size)).recording
    storage.put(BUCKET, recording.storage_key, stored_size if stored_size is not None else size)
    return recording


async def test_create_returns_upload_url(
    create: CreateRecording, user: User, project: Project, db: FakeDb
) -> None:
    result = await _create(create, user, project)
    assert result.upload_url.startswith(f"http://storage.test/{BUCKET}/")
    assert result.recording.status is RecordingStatus.CREATED
    assert [type(e) for e in db.events] == [RecordingCreated]


async def test_create_in_foreign_project_is_not_found(
    create: CreateRecording, project: Project
) -> None:
    stranger = User.register("b@example.com", "hash", FREE_PLAN)
    with pytest.raises(NotFoundError):
        await _create(create, stranger, project)


async def test_create_rejects_oversized_file(
    create: CreateRecording, user: User, project: Project
) -> None:
    with pytest.raises(FileTooLargeError):
        await _create(create, user, project, size=600 * 1024 * 1024)


async def test_complete_moves_to_processing(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    user: User,
    project: Project,
    db: FakeDb,
) -> None:
    recording = await _uploaded(create, storage, user, project)
    result = await complete.execute(user.id, recording.id)
    assert result.status is RecordingStatus.PROCESSING
    assert result.duration_sec == 60.0
    assert RecordingProcessingRequested in {type(e) for e in db.events}


async def test_complete_is_idempotent(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    user: User,
    project: Project,
    db: FakeDb,
) -> None:
    recording = await _uploaded(create, storage, user, project)
    await complete.execute(user.id, recording.id)
    again = await complete.execute(user.id, recording.id)
    assert again.status is RecordingStatus.PROCESSING
    assert sum(isinstance(e, RecordingProcessingRequested) for e in db.events) == 1


async def test_complete_without_upload_keeps_state(
    create: CreateRecording, complete: CompleteUpload, user: User, project: Project, db: FakeDb
) -> None:
    recording = (await _create(create, user, project)).recording
    with pytest.raises(UploadMissingError):
        await complete.execute(user.id, recording.id)
    assert db.recordings[recording.id].status is RecordingStatus.CREATED


async def test_complete_size_mismatch_fails_recording(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    user: User,
    project: Project,
    db: FakeDb,
) -> None:
    recording = await _uploaded(create, storage, user, project, size=1024, stored_size=999)
    with pytest.raises(InvalidMediaError):
        await complete.execute(user.id, recording.id)
    assert db.recordings[recording.id].status is RecordingStatus.FAILED


async def test_complete_invalid_media_fails_recording(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    probe: FakeMediaProbe,
    user: User,
    project: Project,
    db: FakeDb,
) -> None:
    recording = await _uploaded(create, storage, user, project)
    probe.error = "File has no audio track"
    with pytest.raises(InvalidMediaError):
        await complete.execute(user.id, recording.id)
    assert db.recordings[recording.id].status is RecordingStatus.FAILED


async def test_complete_too_long_fails_recording(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    probe: FakeMediaProbe,
    user: User,
    project: Project,
    db: FakeDb,
) -> None:
    recording = await _uploaded(create, storage, user, project)
    probe.duration_sec = MAX_DURATION_SEC + 1
    with pytest.raises(InvalidMediaError):
        await complete.execute(user.id, recording.id)
    assert db.recordings[recording.id].status is RecordingStatus.FAILED


async def test_complete_foreign_recording_is_not_found(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    user: User,
    project: Project,
) -> None:
    recording = await _uploaded(create, storage, user, project)
    stranger = User.register("b@example.com", "hash", FREE_PLAN)
    with pytest.raises(NotFoundError):
        await complete.execute(stranger.id, recording.id)


async def test_delete_rules(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    uow_factory: UnitOfWorkFactory,
    user: User,
    project: Project,
) -> None:
    delete = DeleteRecording(uow_factory)
    get = GetRecording(uow_factory)

    processing = await _uploaded(create, storage, user, project)
    await complete.execute(user.id, processing.id)
    with pytest.raises(InvalidStateError):
        await delete.execute(user.id, processing.id)

    fresh = (await _create(create, user, project)).recording
    await delete.execute(user.id, fresh.id)
    with pytest.raises(NotFoundError):
        await get.execute(user.id, fresh.id)


async def test_complete_charges_quota_and_creates_stages(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    user: User,
    project: Project,
    db: FakeDb,
) -> None:
    recording = await _uploaded(create, storage, user, project)
    await complete.execute(user.id, recording.id)
    assert db.usage[(user.id, usage_period(datetime.now(UTC)))] == 1  # 60 s -> 1 minute
    assert len(db.stages) == len(PIPELINE_STAGES)


async def test_quota_exceeded_fails_before_processing(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    user: User,
    project: Project,
    db: FakeDb,
) -> None:
    db.usage[(user.id, usage_period(datetime.now(UTC)))] = 300  # the free plan limit
    recording = await _uploaded(create, storage, user, project)
    with pytest.raises(QuotaExceededError):
        await complete.execute(user.id, recording.id)
    assert db.recordings[recording.id].status is RecordingStatus.FAILED
    assert db.stages == {}
    assert RecordingProcessingRequested not in {type(e) for e in db.events}


async def test_cancel_then_retry(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    uow_factory: UnitOfWorkFactory,
    user: User,
    project: Project,
    db: FakeDb,
) -> None:
    cancel, retry = CancelRecording(uow_factory), RetryRecording(uow_factory)
    recording = await _uploaded(create, storage, user, project)
    await complete.execute(user.id, recording.id)
    assert db.recordings[recording.id].run == 1

    assert (await cancel.execute(user.id, recording.id)).status is RecordingStatus.CANCELLED
    with pytest.raises(InvalidStateError):  # cancel works only from `processing`
        await cancel.execute(user.id, recording.id)

    retried = await retry.execute(user.id, recording.id)
    assert retried.status is RecordingStatus.PROCESSING
    assert retried.run == 2  # tasks of run 1 are stale now
    assert (await retry.execute(user.id, recording.id)).run == 2  # idempotent
    requested = [e for e in db.events if isinstance(e, RecordingProcessingRequested)]
    assert [e.run for e in requested] == [1, 2]


async def test_retry_and_cancel_reject_wrong_states(
    create: CreateRecording,
    uow_factory: UnitOfWorkFactory,
    user: User,
    project: Project,
) -> None:
    fresh = (await _create(create, user, project)).recording
    with pytest.raises(InvalidStateError):
        await RetryRecording(uow_factory).execute(user.id, fresh.id)
    with pytest.raises(InvalidStateError):
        await CancelRecording(uow_factory).execute(user.id, fresh.id)


async def test_retry_is_rejected_after_a_rejected_upload(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    uow_factory: UnitOfWorkFactory,
    user: User,
    project: Project,
    db: FakeDb,
) -> None:
    db.usage[(user.id, usage_period(datetime.now(UTC)))] = 300
    recording = await _uploaded(create, storage, user, project)
    with pytest.raises(QuotaExceededError):
        await complete.execute(user.id, recording.id)

    with pytest.raises(InvalidStateError):  # retry must not bypass the quota check
        await RetryRecording(uow_factory).execute(user.id, recording.id)


async def test_complete_validates_and_processes_a_private_copy(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    probe: FakeMediaProbe,
    user: User,
    project: Project,
) -> None:
    recording = await _uploaded(create, storage, user, project)
    await complete.execute(user.id, recording.id)
    assert ("artifacts", artifact_key(user.id, recording.id, SOURCE_COPY)) in storage.objects
    assert probe.content_types == ["audio/wav"]  # the demuxer is chosen from the declared type


async def test_replacing_the_upload_during_validation_fails_the_recording(
    create: CreateRecording,
    complete: CompleteUpload,
    storage: FakeObjectStorage,
    user: User,
    project: Project,
    db: FakeDb,
) -> None:
    recording = await _uploaded(create, storage, user, project)
    storage.overwrite_before_copy = True
    with pytest.raises(InvalidMediaError):
        await complete.execute(user.id, recording.id)
    assert db.recordings[recording.id].status is RecordingStatus.FAILED
    assert db.stages == {}
    assert RecordingProcessingRequested not in {type(e) for e in db.events}
