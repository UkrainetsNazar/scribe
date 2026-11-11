import tempfile
from pathlib import Path
from typing import Any

import orjson

from scribe.application.interfaces import ObjectStorage
from scribe.application.pipeline import StageContext, StageHandler, StageResult
from scribe.application.pipeline_ports import (
    AudioProcessor,
    Summarizer,
    Transcriber,
    TranscriptIndex,
)
from scribe.application.wav import wav_duration
from scribe.domain.pipeline import (
    CHUNK_MANIFEST,
    EXTRACTED_AUDIO,
    SEGMENTS,
    SUBTITLES_SRT,
    SUBTITLES_VTT,
    SUMMARY,
    PermanentStageError,
    StageName,
    artifact_key,
    chunk_name,
    chunk_transcript_name,
)
from scribe.domain.recordings import Recording
from scribe.domain.subtitles import to_srt, to_vtt
from scribe.domain.transcript import Segment, Summary, Transcription


class _ArtifactHandler(StageHandler):
    def __init__(self, storage: ObjectStorage, artifacts_bucket: str) -> None:
        self._storage = storage
        self._artifacts = artifacts_bucket

    @staticmethod
    def _key(recording: Recording, name: str) -> str:
        return artifact_key(recording.owner_id, recording.id, name)

    async def _read_json(self, recording: Recording, name: str) -> dict[str, Any]:
        data = await self._storage.get_bytes(self._artifacts, self._key(recording, name))
        if data is None:
            raise PermanentStageError(f"Artifact '{name}' is missing")
        parsed = orjson.loads(data)
        if not isinstance(parsed, dict):
            raise PermanentStageError(f"Artifact '{name}' is malformed")
        return parsed

    async def _write_json(self, recording: Recording, name: str, payload: dict[str, Any]) -> None:
        await self._storage.put_bytes(
            self._artifacts, self._key(recording, name), orjson.dumps(payload), "application/json"
        )

    async def _download(self, recording: Recording, name: str, target: Path) -> None:
        try:
            await self._storage.download_file(self._artifacts, self._key(recording, name), target)
        except FileNotFoundError as exc:
            raise PermanentStageError(f"Artifact '{name}' is missing") from exc


class ExtractAudioHandler(_ArtifactHandler):
    name = StageName.EXTRACT_AUDIO

    def __init__(
        self,
        storage: ObjectStorage,
        audio: AudioProcessor,
        uploads_bucket: str,
        artifacts_bucket: str,
    ) -> None:
        super().__init__(storage, artifacts_bucket)
        self._audio = audio
        self._uploads = uploads_bucket

    async def execute(self, ctx: StageContext) -> StageResult | None:
        recording = ctx.recording
        with tempfile.TemporaryDirectory(prefix="scribe-") as tmp:
            source = Path(tmp) / "source"
            try:
                await self._storage.download_file(self._uploads, recording.storage_key, source)
            except FileNotFoundError as exc:
                raise PermanentStageError("Uploaded file is missing") from exc
            await ctx.progress(30)
            extracted = Path(tmp) / "extracted.flac"
            await self._audio.extract_audio(source, extracted)
            await ctx.progress(80)
            await self._storage.upload_file(
                self._artifacts, self._key(recording, EXTRACTED_AUDIO), extracted, "audio/flac"
            )
        return None


class NormalizeAndChunkHandler(_ArtifactHandler):
    name = StageName.NORMALIZE_AND_CHUNK

    def __init__(
        self,
        storage: ObjectStorage,
        audio: AudioProcessor,
        artifacts_bucket: str,
        chunk_seconds: int,
    ) -> None:
        super().__init__(storage, artifacts_bucket)
        self._audio = audio
        self._chunk_seconds = chunk_seconds

    async def execute(self, ctx: StageContext) -> StageResult | None:
        recording = ctx.recording
        with tempfile.TemporaryDirectory(prefix="scribe-") as tmp:
            extracted = Path(tmp) / "extracted.flac"
            await self._download(recording, EXTRACTED_AUDIO, extracted)
            await ctx.progress(10)

            out_dir = Path(tmp) / "chunks"
            out_dir.mkdir()
            paths = await self._audio.split_into_chunks(extracted, out_dir, self._chunk_seconds)
            await ctx.progress(50)

            chunks: list[dict[str, Any]] = []
            start = 0.0
            for index, path in enumerate(paths):
                try:
                    duration = wav_duration(path)
                except (OSError, EOFError, ValueError) as exc:
                    raise PermanentStageError(f"Chunk {index} is not a valid WAV") from exc
                await self._storage.upload_file(
                    self._artifacts, self._key(recording, chunk_name(index)), path, "audio/wav"
                )
                chunks.append({"index": index, "start": start, "duration": duration})
                start += duration
                await ctx.progress(50 + int((index + 1) / len(paths) * 50))
        await self._write_json(recording, CHUNK_MANIFEST, {"chunks": chunks})
        return None


class TranscribeHandler(_ArtifactHandler):
    name = StageName.TRANSCRIBE

    def __init__(
        self, storage: ObjectStorage, transcriber: Transcriber, artifacts_bucket: str
    ) -> None:
        super().__init__(storage, artifacts_bucket)
        self._transcriber = transcriber

    async def execute(self, ctx: StageContext) -> StageResult | None:
        recording = ctx.recording
        manifest = await self._read_json(recording, CHUNK_MANIFEST)
        chunks: list[dict[str, Any]] = manifest["chunks"]
        if not chunks:
            raise PermanentStageError("The recording has no audio chunks")

        # Auto-detect on the first chunk only, then reuse the language for the rest
        language = None if recording.language == "auto" else recording.language
        segments: list[Segment] = []
        model = self._transcriber.model_name
        for position, chunk in enumerate(chunks):
            index = int(chunk["index"])
            result_name = chunk_transcript_name(index)
            cached = await self._storage.get_bytes(
                self._artifacts, self._key(recording, result_name)
            )
            if cached is not None:  # resume: this chunk was transcribed by an earlier attempt
                transcription = Transcription.from_dict(orjson.loads(cached))
            else:
                with tempfile.TemporaryDirectory(prefix="scribe-") as tmp:
                    path = Path(tmp) / Path(chunk_name(index)).name
                    await self._download(recording, chunk_name(index), path)
                    transcription = await self._transcriber.transcribe(path, language)
                await self._write_json(recording, result_name, transcription.to_dict())

            language = language or transcription.language
            model = transcription.model
            offset = float(chunk["start"])
            segments.extend(s.shifted(offset) for s in transcription.segments if s.text.strip())
            await ctx.progress(int((position + 1) / len(chunks) * 100))

        detected = language or "en"
        merged = Transcription(language=detected, model=model, segments=segments)
        await self._write_json(recording, SEGMENTS, merged.to_dict())
        return StageResult(language=detected)


class BuildSubtitlesHandler(_ArtifactHandler):
    name = StageName.BUILD_SUBTITLES

    async def execute(self, ctx: StageContext) -> StageResult | None:
        recording = ctx.recording
        transcription = Transcription.from_dict(await self._read_json(recording, SEGMENTS))
        for name, content, mime in (
            (SUBTITLES_SRT, to_srt(transcription.segments), "application/x-subrip"),
            (SUBTITLES_VTT, to_vtt(transcription.segments), "text/vtt"),
        ):
            await self._storage.put_bytes(
                self._artifacts, self._key(recording, name), content.encode(), mime
            )
        return None


class SummarizeHandler(_ArtifactHandler):
    name = StageName.SUMMARIZE

    def __init__(
        self, storage: ObjectStorage, summarizer: Summarizer, artifacts_bucket: str
    ) -> None:
        super().__init__(storage, artifacts_bucket)
        self._summarizer = summarizer

    async def execute(self, ctx: StageContext) -> StageResult | None:
        transcription = Transcription.from_dict(await self._read_json(ctx.recording, SEGMENTS))
        summary = await self._summarizer.summarize(transcription)
        await self._write_json(ctx.recording, SUMMARY, summary.to_dict())
        return None


class IndexHandler(_ArtifactHandler):
    name = StageName.INDEX

    def __init__(
        self, storage: ObjectStorage, index: TranscriptIndex, artifacts_bucket: str
    ) -> None:
        super().__init__(storage, artifacts_bucket)
        self._index = index

    async def execute(self, ctx: StageContext) -> StageResult | None:
        recording = ctx.recording
        transcription = Transcription.from_dict(await self._read_json(recording, SEGMENTS))
        summary = Summary.from_dict(await self._read_json(recording, SUMMARY))
        await self._index.index(
            recording_id=recording.id,
            owner_id=recording.owner_id,
            transcription=transcription,
            summary=summary,
        )
        return None
