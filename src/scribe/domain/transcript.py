from dataclasses import dataclass, replace
from typing import Any, Self


@dataclass(frozen=True, slots=True)
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    confidence: float = 1.0

    def shifted(self, offset: float) -> Self:
        return replace(self, start=self.start + offset, end=self.end + offset)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "speaker": self.speaker,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data["text"]),
            speaker=data.get("speaker"),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass(frozen=True, slots=True)
class Transcription:
    language: str
    model: str
    segments: list[Segment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "model": self.model,
            "segments": [{"idx": i, **s.to_dict()} for i, s in enumerate(self.segments)],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            language=str(data["language"]),
            model=str(data["model"]),
            segments=[Segment.from_dict(s) for s in data["segments"]],
        )


@dataclass(frozen=True, slots=True)
class Summary:
    summary: str
    topics: list[str]
    action_items: list[str]
    model: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "topics": self.topics,
            "action_items": self.action_items,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            summary=str(data["summary"]),
            topics=[str(t) for t in data["topics"]],
            action_items=[str(a) for a in data["action_items"]],
            model=str(data["model"]),
        )
