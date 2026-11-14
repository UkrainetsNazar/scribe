from typing import Any
from uuid import uuid4

import orjson
import pytest

from scribe.application.interfaces import UnitOfWorkFactory
from scribe.application.use_cases.exports import CreateExport, GetExportLink
from scribe.application.use_cases.results import GetSummary, GetTranscript, SearchTranscripts
from scribe.domain.errors import (
    BadRequestError,
    DomainValidationError,
    InvalidStateError,
    NotFoundError,
)
from scribe.domain.exports import ExportFormat
from scribe.domain.pipeline import SEGMENTS, artifact_key
from scribe.domain.projects import Project
from scribe.domain.recordings import Recording, RecordingStatus
from scribe.domain.transcript import Segment, Summary, Transcription
from tests.unit.fakes import FakeDb, FakeObjectStorage, FakeTranscriptReader
from tests.unit.test_domain_recording import make_recording

ARTIFACTS, EXPORTS = "artifacts", "exports"


@pytest.fixture
def reader() -> FakeTranscriptReader:
    return FakeTranscriptReader()


def _recording(
    db: FakeDb,
    reader: FakeTranscriptReader,
    texts: list[str],
    status: RecordingStatus = RecordingStatus.COMPLETED,
    **overrides: Any,
) -> Recording:
    recording = make_recording(**overrides)
    recording.status = status
    recording.pull_events()
    db.recordings[recording.id] = recording
    reader.segments[recording.id] = [
        Segment(i * 2.0, i * 2.0 + 2.0, text) for i, text in enumerate(texts)
    ]
    return recording


async def test_transcript_pages_and_start_filter(
    db: FakeDb, reader: FakeTranscriptReader, uow_factory: UnitOfWorkFactory
) -> None:
    recording = _recording(db, reader, [f"line {i}" for i in range(5)])
    get = GetTranscript(uow_factory, reader)
    owner, rid = recording.owner_id, recording.id

    first = await get.execute(owner, rid, 0.0, 2, None)
    assert [i.idx for i in first.items] == [0, 1]
    assert first.next_cursor is not None
    assert first.language == recording.language

    second = await get.execute(owner, rid, 0.0, 2, first.next_cursor)
    assert [i.idx for i in second.items] == [2, 3]
    assert second.next_cursor is not None

    last = await get.execute(owner, rid, 0.0, 2, second.next_cursor)
    assert [i.idx for i in last.items] == [4]
    assert last.next_cursor is None

    later = await get.execute(owner, rid, 5.0, 10, None)  # segment 2 spans 4..6
    assert [i.idx for i in later.items] == [2, 3, 4]


async def test_results_require_a_completed_recording_of_the_caller(
    db: FakeDb, reader: FakeTranscriptReader, uow_factory: UnitOfWorkFactory
) -> None:
    processing = _recording(db, reader, ["x"], status=RecordingStatus.PROCESSING)
    done = _recording(db, reader, ["x"])
    get, summary = GetTranscript(uow_factory, reader), GetSummary(uow_factory, reader)

    with pytest.raises(InvalidStateError):
        await get.execute(processing.owner_id, processing.id, 0.0, 10, None)
    with pytest.raises(InvalidStateError):
        await summary.execute(processing.owner_id, processing.id)
    with pytest.raises(NotFoundError):
        await get.execute(uuid4(), done.id, 0.0, 10, None)  # somebody else's recording
    with pytest.raises(BadRequestError):
        await get.execute(done.owner_id, done.id, 0.0, 10, "garbage")


async def test_summary(
    db: FakeDb, reader: FakeTranscriptReader, uow_factory: UnitOfWorkFactory
) -> None:
    recording = _recording(db, reader, ["x"])
    get = GetSummary(uow_factory, reader)
    with pytest.raises(NotFoundError):
        await get.execute(recording.owner_id, recording.id)

    reader.summaries[recording.id] = Summary("short", ["topic"], ["do it"], "m")
    assert (await get.execute(recording.owner_id, recording.id)).topics == ["topic"]


async def test_search_covers_only_completed_recordings_of_the_caller(
    db: FakeDb, reader: FakeTranscriptReader, uow_factory: UnitOfWorkFactory
) -> None:
    owner = uuid4()
    first = Project.create(owner, "A")
    second = Project.create(owner, "B")
    db.projects.update({first.id: first, second.id: second})
    in_a = _recording(db, reader, ["the budget is tight"], owner_id=owner, project_id=first.id)
    in_b = _recording(db, reader, ["budget for next year"], owner_id=owner, project_id=second.id)
    _recording(db, reader, ["budget"], owner_id=owner, status=RecordingStatus.PROCESSING)
    _recording(db, reader, ["budget"], owner_id=owner, status=RecordingStatus.DELETED)
    _recording(db, reader, ["budget"])  # another user
    search = SearchTranscripts(uow_factory, reader)

    found = await search.execute(owner, "  budget ", None, 20)
    assert {r.recording_id for r in found} == {in_a.id, in_b.id}
    assert found[0].snippet in {"the budget is tight", "budget for next year"}

    only_a = await search.execute(owner, "budget", first.id, 20)
    assert [r.recording_id for r in only_a] == [in_a.id]
    assert len(await search.execute(owner, "budget", None, 1)) == 1

    with pytest.raises(NotFoundError):
        await search.execute(uuid4(), "budget", first.id, 20)  # somebody else's project


@pytest.mark.parametrize("query", ["", " a ", "x" * 201])
async def test_search_rejects_bad_queries(
    query: str, reader: FakeTranscriptReader, uow_factory: UnitOfWorkFactory
) -> None:
    with pytest.raises(DomainValidationError):
        await SearchTranscripts(uow_factory, reader).execute(uuid4(), query, None, 20)


async def _seed_transcript(storage: FakeObjectStorage, recording: Recording) -> None:
    transcription = Transcription("en", "m", [Segment(0.0, 1.0, "Hello world")])
    await storage.put_bytes(
        ARTIFACTS,
        artifact_key(recording.owner_id, recording.id, SEGMENTS),
        orjson.dumps(transcription.to_dict()),
        "application/json",
    )


async def test_export_is_built_stored_and_linked(
    db: FakeDb, reader: FakeTranscriptReader, uow_factory: UnitOfWorkFactory
) -> None:
    storage = FakeObjectStorage()
    recording = _recording(db, reader, ["x"], title="Weekly sync")
    await _seed_transcript(storage, recording)
    create = CreateExport(uow_factory, storage, ARTIFACTS, EXPORTS)
    link = GetExportLink(uow_factory, storage, EXPORTS, ttl_min=15)
    owner, rid = recording.owner_id, recording.id

    with pytest.raises(NotFoundError):  # nothing has been created yet
        await link.execute(owner, rid, ExportFormat.SRT)

    export = await create.execute(owner, rid, ExportFormat.SRT)
    assert (
        storage.blobs[(EXPORTS, export.storage_key)]
        == b"1\n00:00:00,000 --> 00:00:01,000\nHello world\n"
    )

    again = await create.execute(owner, rid, ExportFormat.SRT)
    assert again.id == export.id  # repeating the call rebuilds the same row

    result = await link.execute(owner, rid, ExportFormat.SRT)
    assert result.url.endswith("?name=Weekly_sync.srt")
    with pytest.raises(NotFoundError):  # other formats are separate exports
        await link.execute(owner, rid, ExportFormat.TXT)


async def test_export_requires_a_completed_recording(
    db: FakeDb, reader: FakeTranscriptReader, uow_factory: UnitOfWorkFactory
) -> None:
    storage = FakeObjectStorage()
    processing = _recording(db, reader, ["x"], status=RecordingStatus.PROCESSING)
    create = CreateExport(uow_factory, storage, ARTIFACTS, EXPORTS)

    with pytest.raises(InvalidStateError):
        await create.execute(processing.owner_id, processing.id, ExportFormat.TXT)

    done = _recording(db, reader, ["x"])  # completed, but the artifact is gone
    with pytest.raises(NotFoundError):
        await create.execute(done.owner_id, done.id, ExportFormat.TXT)
