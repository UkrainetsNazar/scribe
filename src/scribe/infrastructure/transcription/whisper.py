import asyncio
import math
import threading
from pathlib import Path
from typing import Any

from scribe.application.pipeline_ports import Transcriber
from scribe.domain.pipeline import PermanentStageError
from scribe.domain.transcript import Segment, Transcription

# Loading a model takes seconds: keep it per worker process, not per task
_MODELS: dict[tuple[str, str, str], Any] = {}
_MODELS_LOCK = threading.Lock()


def _confidence(avg_logprob: float) -> float:
    return max(0.0, min(1.0, math.exp(avg_logprob)))


class FasterWhisperTranscriber(Transcriber):
    def __init__(self, model_size: str, device: str, compute_type: str, beam_size: int) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._beam_size = beam_size

    @property
    def model_name(self) -> str:
        return f"faster-whisper-{self._model_size}"

    async def transcribe(self, audio_path: Path, language: str | None) -> Transcription:
        return await asyncio.to_thread(self._transcribe, audio_path, language)

    def _load_model(self) -> Any:
        key = (self._model_size, self._device, self._compute_type)
        with _MODELS_LOCK:
            model = _MODELS.get(key)
            if model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise PermanentStageError(
                        "faster-whisper is not installed (uv sync --extra whisper)"
                    ) from exc
                model = WhisperModel(
                    self._model_size, device=self._device, compute_type=self._compute_type
                )
                _MODELS[key] = model
            return model

    def _transcribe(self, audio_path: Path, language: str | None) -> Transcription:
        model = self._load_model()
        raw_segments, info = model.transcribe(
            str(audio_path), language=language, beam_size=self._beam_size, vad_filter=True
        )
        segments = [  # the generator does the actual decoding while it is consumed
            Segment(
                start=float(s.start),
                end=float(s.end),
                text=s.text.strip(),
                confidence=_confidence(float(s.avg_logprob)),
            )
            for s in raw_segments
            if s.text.strip()
        ]
        return Transcription(language=str(info.language), model=self.model_name, segments=segments)
