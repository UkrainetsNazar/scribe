from abc import ABC, abstractmethod
from pathlib import Path
from uuid import UUID

from scribe.domain.transcript import Summary, Transcription


class AudioProcessor(ABC):
    @abstractmethod
    async def extract_audio(self, source: Path, target: Path) -> None:
        """Extracts the first audio track losslessly. Raises PermanentStageError."""

    @abstractmethod
    async def split_into_chunks(self, source: Path, out_dir: Path, chunk_sec: int) -> list[Path]:
        """Normalizes loudness, converts to 16 kHz mono WAV and splits into fixed-size chunks."""


class Transcriber(ABC):
    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @abstractmethod
    async def transcribe(self, audio_path: Path, language: str | None) -> Transcription:
        """`language=None` means auto-detect. Timestamps are relative to the audio file."""


class Summarizer(ABC):
    @abstractmethod
    async def summarize(self, transcription: Transcription) -> Summary: ...


class TranscriptIndex(ABC):
    @abstractmethod
    async def index(
        self,
        *,
        recording_id: UUID,
        owner_id: UUID,
        transcription: Transcription,
        summary: Summary | None,
    ) -> None:
        """Idempotent: a repeated call for the same recording replaces its documents."""


class PipelineDispatcher(ABC):
    @abstractmethod
    async def dispatch(self, recording_id: UUID, owner_id: UUID, run: int) -> None:
        """Queues the first pipeline stage."""
