import re
from uuid import uuid4

import pytest

from scribe.domain.errors import QuotaExceededError
from scribe.domain.pipeline import (
    PIPELINE_STAGES,
    ProcessingStage,
    StageName,
    StageStatus,
    next_stage,
)
from scribe.domain.quota import billable_minutes, ensure_within_quota
from scribe.domain.subtitles import to_srt, to_vtt
from scribe.domain.transcript import Segment, Transcription
from scribe.domain.users import Plan
from scribe.infrastructure.summarization.extractive import ExtractiveSummarizer

PLAN = Plan(id=1, name="free", monthly_minutes=300, max_file_mb=500)


def test_stage_lifecycle() -> None:
    stage = ProcessingStage(recording_id=uuid4(), name=StageName.TRANSCRIBE)
    stage.start()
    assert (stage.status, stage.attempts) == (StageStatus.RUNNING, 1)
    stage.set_progress(150)
    assert stage.progress == 100
    stage.fail("boom")
    assert stage.status is StageStatus.FAILED and stage.error == "boom"
    stage.start()
    assert (stage.attempts, stage.error) == (2, None)
    stage.complete()
    assert (stage.status, stage.progress) == (StageStatus.COMPLETED, 100)


def test_stage_order() -> None:
    assert PIPELINE_STAGES[0] is StageName.EXTRACT_AUDIO
    assert next_stage(StageName.EXTRACT_AUDIO) is StageName.NORMALIZE_AND_CHUNK
    assert next_stage(StageName.INDEX) is None


def test_billable_minutes_are_rounded_up() -> None:
    assert billable_minutes(0.1) == 1
    assert billable_minutes(60) == 1
    assert billable_minutes(61) == 2


def test_quota_boundary() -> None:
    assert ensure_within_quota(PLAN, used_minutes=299, duration_sec=60) == 1
    with pytest.raises(QuotaExceededError):
        ensure_within_quota(PLAN, used_minutes=300, duration_sec=1)


SEGMENTS = [Segment(0.0, 1.5, "Hello world"), Segment(61.25, 3661.0, "  Second   line  ")]


def test_srt_format() -> None:
    assert to_srt(SEGMENTS) == (
        "1\n00:00:00,000 --> 00:00:01,500\nHello world\n\n"
        "2\n00:01:01,250 --> 01:01:01,000\nSecond line\n"
    )
    cues = re.findall(r"(\d+)\n(\S+) --> (\S+)\n(.+)", to_srt(SEGMENTS))
    assert [c[0] for c in cues] == ["1", "2"]


def test_vtt_format_and_cleanup() -> None:
    vtt = to_vtt([*SEGMENTS, Segment(5.0, 6.0, "   "), Segment(7.0, 8.0, "a --> b")])
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nHello world\n")
    assert "a -> b" in vtt  # `-->` inside a cue would break parsers
    assert vtt.count(" --> ") == 3  # the blank segment is dropped


async def test_extractive_summary() -> None:
    text = (
        "The budget is the main topic. We discussed the budget for next year. "
        "We need to send the budget report by Friday. Lunch was great."
    )
    transcription = Transcription("en", "m", [Segment(0.0, 10.0, text)])
    summary = await ExtractiveSummarizer().summarize(transcription)
    assert "budget" in summary.summary
    assert summary.topics[0] == "budget"
    assert summary.action_items == ["We need to send the budget report by Friday."]


async def test_extractive_summary_of_empty_transcript() -> None:
    summary = await ExtractiveSummarizer().summarize(Transcription("en", "m", []))
    assert (summary.summary, summary.topics, summary.action_items) == ("", [], [])
