from collections.abc import Sequence

from scribe.domain.transcript import Segment


def _timestamp(seconds: float, separator: str) -> str:
    total_ms = round(max(seconds, 0.0) * 1000)
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{ms:03}"


def _cues(segments: Sequence[Segment], separator: str) -> list[tuple[str, str, str]]:
    cues = []
    for segment in segments:
        text = " ".join(segment.text.split()).replace("-->", "->")
        if not text:
            continue
        end = max(segment.end, segment.start + 0.001)
        cues.append((_timestamp(segment.start, separator), _timestamp(end, separator), text))
    return cues


def to_srt(segments: Sequence[Segment]) -> str:
    blocks = [
        f"{number}\n{start} --> {end}\n{text}\n"
        for number, (start, end, text) in enumerate(_cues(segments, ","), start=1)
    ]
    return "\n".join(blocks)


def to_vtt(segments: Sequence[Segment]) -> str:
    blocks = [f"{start} --> {end}\n{text}\n" for start, end, text in _cues(segments, ".")]
    return "WEBVTT\n\n" + "\n".join(blocks)
