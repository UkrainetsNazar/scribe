import json
from uuid import uuid4

import pytest

from scribe.domain.exports import (
    ExportFormat,
    export_filename,
    export_key,
    render_export,
    to_txt,
)
from scribe.domain.search import make_snippet
from scribe.domain.subtitles import to_srt
from scribe.domain.transcript import Segment, Transcription

SEGMENTS = [
    Segment(0.0, 1.0, "Hello"),
    Segment(3725.5, 3727.0, "  Second   line "),
    Segment(5.0, 6.0, "  "),
]
TRANSCRIPTION = Transcription("en", "m", SEGMENTS)


def test_txt_has_clock_prefixes_and_skips_blank_segments() -> None:
    assert to_txt(SEGMENTS) == "[00:00:00] Hello\n[01:02:05] Second line\n"
    assert to_txt([]) == ""


def test_srt_and_vtt_reuse_the_subtitle_builders() -> None:
    kwargs = {"recording_id": uuid4(), "title": "T", "duration_sec": 10.0}
    assert render_export(ExportFormat.SRT, TRANSCRIPTION, **kwargs).decode() == to_srt(SEGMENTS)
    assert render_export(ExportFormat.VTT, TRANSCRIPTION, **kwargs).startswith(b"WEBVTT")


def test_json_export_keeps_unicode_and_metadata() -> None:
    recording_id = uuid4()
    transcription = Transcription("uk", "m", [Segment(0.0, 1.0, "Привіт")])
    raw = render_export(
        ExportFormat.JSON,
        transcription,
        recording_id=recording_id,
        title="Зустріч",
        duration_sec=1.0,
    )
    assert "Привіт" in raw.decode()  # not escaped to \uXXXX
    data = json.loads(raw)
    assert (data["recording_id"], data["title"], data["language"]) == (
        str(recording_id),
        "Зустріч",
        "uk",
    )
    assert data["segments"][0] == {
        "idx": 0,
        "start": 0.0,
        "end": 1.0,
        "text": "Привіт",
        "speaker": None,
        "confidence": 1.0,
    }


@pytest.mark.parametrize(
    ("title", "fmt", "expected"),
    [
        ("Weekly sync", ExportFormat.JSON, "Weekly_sync.json"),
        ("Зустріч", ExportFormat.TXT, "transcript.txt"),  # nothing usable is left
        ("../../etc/passwd", ExportFormat.SRT, "passwd.srt"),
    ],
)
def test_export_filename(title: str, fmt: ExportFormat, expected: str) -> None:
    assert export_filename(title, fmt) == expected


def test_export_key() -> None:
    owner, recording = uuid4(), uuid4()
    assert (
        export_key(owner, recording, ExportFormat.VTT)
        == f"{owner}/{recording}/export/transcript.vtt"
    )


def test_snippet_of_a_short_text_is_the_text() -> None:
    assert make_snippet("  Short   text ", "short") == "Short text"


def test_snippet_is_a_window_around_the_match() -> None:
    text = ("lorem ipsum " * 30) + "BUDGET is here " + ("dolor sit " * 30)
    snippet = make_snippet(text, "budget", width=80)
    assert "BUDGET" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) <= 80 + 2


def test_snippet_without_a_match_starts_at_the_beginning() -> None:
    snippet = make_snippet("word " * 100, "zzz", width=40)
    assert snippet.startswith("word") and snippet.endswith("…")
