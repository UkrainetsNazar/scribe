from uuid import uuid4

import pytest

from scribe.application.event_handlers import RecordingCleanupHandler
from scribe.application.inbox import InboundEvent, PermanentHandlerError
from scribe.domain.recordings import Recording, RecordingStatus
from scribe.domain.transcript import Segment, Transcription
from tests.unit.fakes import FakeDb, FakeObjectStorage, FakeTranscriptIndex, FakeUnitOfWork
from tests.unit.test_domain_recording import make_recording

BUCKETS = ["uploads", "artifacts", "exports"]
FILES = (
    ("uploads", "source/a.wav"),
    ("artifacts", "audio/extracted.flac"),
    ("artifacts", "chunks/chunk_0000.wav"),
    ("exports", "export/transcript.srt"),
)


def _recording(db: FakeDb, status: RecordingStatus) -> Recording:
    recording = make_recording()
    recording.status = status
    recording.pull_events()
    db.recordings[recording.id] = recording
    return recording


def _event(recording: Recording) -> InboundEvent:
    return InboundEvent(
        event_id=uuid4(),
        event_type="recording.deleted",
        payload={
            "recording_id": str(recording.id),
            "owner_id": str(recording.owner_id),
            "storage_key": recording.storage_key,
        },
        headers={},
    )


async def _seed(
    storage: FakeObjectStorage, index: FakeTranscriptIndex, recording: Recording
) -> None:
    for bucket, name in FILES:
        await storage.put_bytes(bucket, f"{recording.owner_id}/{recording.id}/{name}", b"x", "x")
    await index.index(
        recording_id=recording.id,
        owner_id=recording.owner_id,
        transcription=Transcription("en", "m", [Segment(0.0, 1.0, "hi")]),
        summary=None,
    )


async def test_cleanup_removes_files_documents_and_the_row(db: FakeDb) -> None:
    storage, index = FakeObjectStorage(), FakeTranscriptIndex()
    target = _recording(db, RecordingStatus.DELETED)
    other = _recording(db, RecordingStatus.COMPLETED)
    await _seed(storage, index, target)
    await _seed(storage, index, other)

    handler = RecordingCleanupHandler(storage, index, BUCKETS)
    await handler.handle(_event(target), FakeUnitOfWork(db))

    assert target.id not in db.recordings and other.id in db.recordings
    assert not any(str(target.id) in key for _, key in storage.blobs)
    assert sum(str(other.id) in key for _, key in storage.blobs) == len(FILES)
    assert [item[0] for item in index.indexed] == [other.id]


async def test_cleanup_is_idempotent(db: FakeDb) -> None:
    storage, index = FakeObjectStorage(), FakeTranscriptIndex()
    target = _recording(db, RecordingStatus.DELETED)
    await _seed(storage, index, target)
    handler = RecordingCleanupHandler(storage, index, BUCKETS)

    await handler.handle(_event(target), FakeUnitOfWork(db))
    await handler.handle(_event(target), FakeUnitOfWork(db))  # nothing is left: no error
    assert storage.blobs == {}


async def test_a_row_that_is_not_deleted_is_never_purged(db: FakeDb) -> None:
    recording = _recording(db, RecordingStatus.COMPLETED)
    handler = RecordingCleanupHandler(FakeObjectStorage(), FakeTranscriptIndex(), BUCKETS)
    await handler.handle(_event(recording), FakeUnitOfWork(db))
    assert recording.id in db.recordings


async def test_malformed_event_is_a_permanent_error(db: FakeDb) -> None:
    broken = InboundEvent(event_id=uuid4(), event_type="recording.deleted", payload={}, headers={})
    handler = RecordingCleanupHandler(FakeObjectStorage(), FakeTranscriptIndex(), BUCKETS)
    with pytest.raises(PermanentHandlerError):
        await handler.handle(broken, FakeUnitOfWork(db))
