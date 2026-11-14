import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from scribe.domain.recordings import safe_filename
from scribe.domain.subtitles import to_srt, to_vtt
from scribe.domain.transcript import Segment, Transcription


class ExportFormat(StrEnum):
    TXT = "txt"
    SRT = "srt"
    VTT = "vtt"
    JSON = "json"


CONTENT_TYPES: dict[ExportFormat, str] = {
    ExportFormat.TXT: "text/plain; charset=utf-8",
    ExportFormat.SRT: "application/x-subrip",
    ExportFormat.VTT: "text/vtt",
    ExportFormat.JSON: "application/json",
}


class ExportStatus(StrEnum):
    READY = "ready"


@dataclass(slots=True)
class Export:
    id: UUID
    recording_id: UUID
    format: ExportFormat
    storage_key: str
    status: ExportStatus = ExportStatus.READY
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(cls, recording_id: UUID, fmt: ExportFormat, storage_key: str) -> Self:
        return cls(id=uuid4(), recording_id=recording_id, format=fmt, storage_key=storage_key)


def export_key(owner_id: UUID, recording_id: UUID, fmt: ExportFormat) -> str:
    return f"{owner_id}/{recording_id}/export/transcript.{fmt.value}"


def export_filename(title: str, fmt: ExportFormat) -> str:
    """The download name: a sanitized title, or `transcript` when nothing usable is left."""
    stem = safe_filename(title).strip("_.-") or "transcript"
    return f"{stem}.{fmt.value}"


def _clock(seconds: float) -> str:
    total = int(max(seconds, 0.0))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def to_txt(segments: Sequence[Segment]) -> str:
    lines = []
    for segment in segments:
        text = " ".join(segment.text.split())
        if text:
            lines.append(f"[{_clock(segment.start)}] {text}")
    return "\n".join(lines) + "\n" if lines else ""


def render_export(
    fmt: ExportFormat,
    transcription: Transcription,
    *,
    recording_id: UUID,
    title: str,
    duration_sec: float | None,
) -> bytes:
    segments = transcription.segments
    match fmt:
        case ExportFormat.TXT:
            return to_txt(segments).encode()
        case ExportFormat.SRT:
            return to_srt(segments).encode()
        case ExportFormat.VTT:
            return to_vtt(segments).encode()
        case ExportFormat.JSON:
            payload = {
                "recording_id": str(recording_id),
                "title": title,
                "duration_sec": duration_sec,
                **transcription.to_dict(),
            }
            return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
