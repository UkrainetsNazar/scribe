from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID


class StageName(StrEnum):
    EXTRACT_AUDIO = "extract_audio"
    NORMALIZE_AND_CHUNK = "normalize_and_chunk"
    TRANSCRIBE = "transcribe"
    BUILD_SUBTITLES = "build_subtitles"
    SUMMARIZE = "summarize"
    INDEX = "index"


PIPELINE_STAGES: tuple[StageName, ...] = tuple(StageName)


def next_stage(stage: StageName) -> StageName | None:
    position = PIPELINE_STAGES.index(stage) + 1
    return PIPELINE_STAGES[position] if position < len(PIPELINE_STAGES) else None


def overall_percent(stage: StageName, percent: int) -> int:
    """Progress of the whole pipeline; every stage weighs the same."""
    position = PIPELINE_STAGES.index(stage)
    return round((position * 100 + max(0, min(100, percent))) / len(PIPELINE_STAGES))


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PermanentStageError(Exception):
    """Retrying cannot help (corrupt file, missing artifact): the recording fails at once."""


# Artifact names, relative to `{owner_id}/{recording_id}/` in the artifacts bucket
EXTRACTED_AUDIO = "audio/extracted.flac"
CHUNK_MANIFEST = "chunks/manifest.json"
SEGMENTS = "transcript/segments.json"
SUBTITLES_SRT = "subtitles/subtitles.srt"
SUBTITLES_VTT = "subtitles/subtitles.vtt"
SUMMARY = "summary/summary.json"


def chunk_name(index: int) -> str:
    return f"chunks/chunk_{index:04d}.wav"


def chunk_transcript_name(index: int) -> str:
    return f"transcript/chunk_{index:04d}.json"


def artifact_key(owner_id: UUID, recording_id: UUID, name: str) -> str:
    return f"{owner_id}/{recording_id}/{name}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class ProcessingStage:
    recording_id: UUID
    name: StageName
    status: StageStatus = StageStatus.PENDING
    progress: int = 0
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    def start(self) -> None:
        self.status = StageStatus.RUNNING
        self.attempts += 1
        self.progress = 0
        self.started_at = _now()
        self.finished_at = None
        self.error = None

    def set_progress(self, percent: int) -> None:
        self.progress = max(0, min(100, percent))

    def complete(self) -> None:
        self.status = StageStatus.COMPLETED
        self.progress = 100
        self.finished_at = _now()
        self.error = None

    def fail(self, error: str) -> None:
        self.status = StageStatus.FAILED
        self.finished_at = _now()
        self.error = error[:2000]
