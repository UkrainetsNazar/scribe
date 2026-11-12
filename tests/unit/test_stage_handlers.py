from uuid import UUID

import orjson

from scribe.application.pipeline import StageContext
from scribe.application.stage_handlers import (
    BuildSubtitlesHandler,
    IndexHandler,
    TranscribeHandler,
)
from scribe.domain.pipeline import (
    CHUNK_MANIFEST,
    SEGMENTS,
    SUBTITLES_SRT,
    SUBTITLES_VTT,
    SUMMARY,
    artifact_key,
    chunk_name,
    chunk_transcript_name,
)
from scribe.domain.recordings import Recording
from scribe.domain.transcript import Segment, Summary, Transcription
from tests.unit.fakes import FakeObjectStorage, FakeTranscriber, FakeTranscriptIndex
from tests.unit.test_domain_recording import make_recording

BUCKET = "artifacts"


def _key(recording: Recording, name: str) -> str:
    return artifact_key(recording.owner_id, recording.id, name)


def _ctx(recording: Recording, progress: list[int]) -> StageContext:
    async def report(percent: int) -> None:
        progress.append(percent)

    return StageContext(recording=recording, progress=report)


async def _seed_chunks(storage: FakeObjectStorage, recording: Recording, count: int) -> None:
    manifest = {"chunks": [{"index": i, "start": i * 10.0, "duration": 10.0} for i in range(count)]}
    await storage.put_bytes(BUCKET, _key(recording, CHUNK_MANIFEST), orjson.dumps(manifest), "x")
    for i in range(count):
        await storage.put_bytes(BUCKET, _key(recording, chunk_name(i)), b"wav", "audio/wav")


async def _merged(storage: FakeObjectStorage, recording: Recording) -> Transcription:
    data = await storage.get_bytes(BUCKET, _key(recording, SEGMENTS))
    assert data is not None
    return Transcription.from_dict(orjson.loads(data))


async def test_transcribe_offsets_segments_and_detects_language_once() -> None:
    storage, transcriber = FakeStorage(), FakeTranscriber(detected_language="uk")
    recording = make_recording(language="auto")
    await _seed_chunks(storage, recording, 2)
    progress: list[int] = []

    result = await TranscribeHandler(storage, transcriber, BUCKET).execute(
        _ctx(recording, progress)
    )

    assert result is not None and result.language == "uk"
    # Detected on the first chunk, then reused
    assert transcriber.calls == [("chunk_0000.wav", None), ("chunk_0001.wav", "uk")]
    merged = await _merged(storage, recording)
    assert [(s.start, s.end, s.text) for s in merged.segments] == [
        (0.0, 1.0, "call 1"),
        (10.0, 11.0, "call 2"),
    ]
    assert progress == [50, 100]


async def test_transcribe_resumes_from_cached_chunks() -> None:
    storage, transcriber = FakeStorage(), FakeTranscriber()
    recording = make_recording(language="auto")
    await _seed_chunks(storage, recording, 2)
    cached = Transcription("uk", "fake", [Segment(0.0, 2.0, "cached")])
    await storage.put_bytes(
        BUCKET, _key(recording, chunk_transcript_name(0)), orjson.dumps(cached.to_dict()), "x"
    )

    await TranscribeHandler(storage, transcriber, BUCKET).execute(_ctx(recording, []))

    assert transcriber.calls == [("chunk_0001.wav", "uk")]  # chunk 0 was not transcribed again
    merged = await _merged(storage, recording)
    assert [s.text for s in merged.segments] == ["cached", "call 1"]


async def test_subtitles_are_built_from_segments() -> None:
    storage = FakeStorage()
    recording = make_recording()
    transcription = Transcription("en", "m", [Segment(0.0, 1.0, "Hello")])
    await storage.put_bytes(
        BUCKET, _key(recording, SEGMENTS), orjson.dumps(transcription.to_dict()), "x"
    )

    await BuildSubtitlesHandler(storage, BUCKET).execute(_ctx(recording, []))

    srt = await storage.get_bytes(BUCKET, _key(recording, SUBTITLES_SRT))
    vtt = await storage.get_bytes(BUCKET, _key(recording, SUBTITLES_VTT))
    assert srt == b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
    assert vtt is not None and vtt.startswith(b"WEBVTT")


async def test_index_receives_transcript_and_summary() -> None:
    storage, index = FakeStorage(), FakeTranscriptIndex()
    recording = make_recording()
    transcription = Transcription("en", "m", [Segment(0.0, 1.0, "Hello")])
    summary = Summary("short", ["hello"], [], "m")
    await storage.put_bytes(
        BUCKET, _key(recording, SEGMENTS), orjson.dumps(transcription.to_dict()), "x"
    )
    await storage.put_bytes(BUCKET, _key(recording, SUMMARY), orjson.dumps(summary.to_dict()), "x")

    await IndexHandler(storage, index, BUCKET).execute(_ctx(recording, []))

    assert index.indexed == [(recording.id, transcription, summary)]
    assert isinstance(index.indexed[0][0], UUID)


FakeStorage = FakeObjectStorage
