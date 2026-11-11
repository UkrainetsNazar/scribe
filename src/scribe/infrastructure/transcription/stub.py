import wave
from pathlib import Path

from scribe.application.pipeline_ports import Transcriber
from scribe.application.wav import wav_duration
from scribe.domain.transcript import Segment, Transcription


class StubTranscriber(Transcriber):
    """Deterministic fake: for development, demos and CI without a model."""

    @property
    def model_name(self) -> str:
        return "stub"

    async def transcribe(self, audio_path: Path, language: str | None) -> Transcription:
        try:
            duration = wav_duration(audio_path)
        except (OSError, EOFError, wave.Error):
            duration = 4.0
        count = max(1, min(int(duration // 2), 50))
        step = duration / count
        segments = [
            Segment(
                start=i * step,
                end=(i + 1) * step,
                text=f"Stub transcript segment {i + 1}.",
                confidence=0.9,
            )
            for i in range(count)
        ]
        return Transcription(language=language or "en", model="stub", segments=segments)
