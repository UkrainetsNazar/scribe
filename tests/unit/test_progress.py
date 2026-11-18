import asyncio
from uuid import UUID, uuid4

import pytest

from scribe.application.event_handlers import ProgressEventHandler
from scribe.application.inbox import InboundEvent, PermanentHandlerError
from scribe.application.progress import (
    BestEffortPublisher,
    Message,
    ProgressPublisher,
    completed_message,
    initial_message,
    is_stale,
    progress_message,
    segment_message,
    snapshot_message,
    status_message,
)
from scribe.domain.pipeline import PIPELINE_STAGES, ProcessingStage, StageName, overall_percent
from scribe.domain.recordings import Recording, RecordingStatus
from scribe.domain.transcript import Segment
from tests.unit.fakes import FakeDb, FakeProgressPublisher, FakeUnitOfWork
from tests.unit.test_domain_recording import make_recording

RID = UUID(int=7)


def _recording(status: RecordingStatus, run: int = 1) -> Recording:
    recording = make_recording()
    recording.status = status
    recording.run = run
    recording.pull_events()
    return recording


def _stages(recording: Recording, completed: int) -> list[ProcessingStage]:
    stages = [ProcessingStage(recording.id, name) for name in PIPELINE_STAGES]
    for stage in stages[:completed]:
        stage.start()
        stage.complete()
    return stages


def test_overall_percent() -> None:
    assert overall_percent(StageName.EXTRACT_AUDIO, 0) == 0
    assert overall_percent(StageName.TRANSCRIBE, 50) == 42  # (2 * 100 + 50) / 6
    assert overall_percent(StageName.INDEX, 100) == 100


def test_progress_message_is_clamped() -> None:
    message = progress_message(RID, 1, StageName.TRANSCRIBE, 150)
    assert message["percent"] == 100
    assert message["stage"] == "transcribe"


def test_snapshot_of_a_running_recording_points_at_the_current_stage() -> None:
    recording = _recording(RecordingStatus.PROCESSING)
    stages = _stages(recording, completed=2)
    stages[2].start()
    stages[2].set_progress(40)

    message = snapshot_message(recording, stages)
    assert message["type"] == "progress"
    assert (message["stage"], message["percent"], message["run"]) == ("transcribe", 40, 1)


def test_snapshot_of_a_failed_recording_names_the_failed_stage() -> None:
    recording = _recording(RecordingStatus.FAILED)
    stages = _stages(recording, completed=2)
    stages[2].start()
    stages[2].fail("boom")

    message = snapshot_message(recording, stages)
    assert message["type"] == "failed"
    assert (message["stage"], message["error"]) == ("transcribe", "boom")


def test_snapshot_of_a_rejected_upload_has_no_stage() -> None:
    message = snapshot_message(_recording(RecordingStatus.FAILED, run=0), [])
    assert message["type"] == "failed"
    assert message["stage"] is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [(RecordingStatus.COMPLETED, "completed"), (RecordingStatus.CANCELLED, "cancelled")],
)
def test_snapshot_of_finished_recordings(status: RecordingStatus, expected: str) -> None:
    assert snapshot_message(_recording(status), [])["type"] == expected


def test_snapshot_before_processing_is_a_status() -> None:
    recording = _recording(RecordingStatus.CREATED, run=0)
    assert snapshot_message(recording, []) == status_message(recording.id, 0, "created")


def test_initial_message_ignores_a_state_of_an_older_run() -> None:
    recording = _recording(RecordingStatus.PROCESSING, run=2)
    current = progress_message(recording.id, 2, StageName.SUMMARIZE, 10)
    previous_run = completed_message(recording.id, 1)

    assert initial_message(current, recording, []) == current
    assert initial_message(previous_run, recording, [])["type"] == "status"
    assert initial_message(None, recording, [])["type"] == "status"


def _event(event_type: str, **payload: object) -> InboundEvent:
    return InboundEvent(
        event_id=uuid4(),
        event_type=event_type,
        payload={"recording_id": str(RID), "owner_id": str(uuid4()), "run": 2, **payload},
        headers={},
    )


@pytest.mark.parametrize(
    ("event_type", "extra", "expected"),
    [
        ("recording.processing_requested", {}, {"type": "status", "status": "processing"}),
        ("recording.completed", {}, {"type": "completed"}),
        ("recording.cancelled", {}, {"type": "cancelled"}),
        (
            "recording.failed",
            {"stage": "transcribe", "reason": "boom"},
            {"type": "failed", "stage": "transcribe", "error": "boom"},
        ),
    ],
)
async def test_status_events_become_messages(
    event_type: str, extra: dict[str, object], expected: Message
) -> None:
    publisher = FakeProgressPublisher()
    handler = ProgressEventHandler(publisher)

    await handler.handle(_event(event_type, **extra), FakeUnitOfWork(FakeDb()))

    ((recording_id, message),) = publisher.states
    assert recording_id == RID
    assert message["run"] == 2
    assert expected.items() <= message.items()


async def test_other_events_are_ignored_and_malformed_ones_are_permanent_errors() -> None:
    publisher = FakeProgressPublisher()
    handler = ProgressEventHandler(publisher)
    uow = FakeUnitOfWork(FakeDb())

    await handler.handle(_event("recording.uploaded"), uow)
    assert publisher.states == []

    broken = InboundEvent(
        event_id=uuid4(), event_type="recording.completed", payload={}, headers={}
    )
    with pytest.raises(PermanentHandlerError):
        await handler.handle(broken, uow)


class BrokenPublisher(ProgressPublisher):
    async def publish_state(self, recording_id: UUID, message: Message) -> None:
        raise RuntimeError("redis is down")

    async def publish_transient(self, recording_id: UUID, message: Message) -> None:
        await asyncio.sleep(10)


async def test_best_effort_publisher_never_raises_or_hangs() -> None:
    publisher = BestEffortPublisher(BrokenPublisher(), timeout_sec=0.05)
    await publisher.publish_state(RID, {})
    await publisher.publish_transient(RID, {})


def test_stale_messages_are_recognized() -> None:
    rid = UUID(int=9)
    last = progress_message(rid, 1, StageName.TRANSCRIBE, 80, attempt=1)

    assert not is_stale(last, None)
    assert is_stale(progress_message(rid, 1, StageName.TRANSCRIBE, 30, attempt=1), last)
    assert is_stale(last, last)  # the same message seen as state and as stream
    assert is_stale(progress_message(rid, 1, StageName.EXTRACT_AUDIO, 100), last)
    assert is_stale(status_message(rid, 1, "processing"), last)
    assert not is_stale(progress_message(rid, 1, StageName.TRANSCRIBE, 0, attempt=2), last)
    assert not is_stale(progress_message(rid, 1, StageName.BUILD_SUBTITLES, 5), last)
    assert not is_stale(completed_message(rid, 1), last)
    assert not is_stale(status_message(rid, 2, "processing"), last)  # a retry starts a new run

    segment = Segment(0.0, 1.0, "x")
    assert is_stale(segment_message(rid, 0, segment), last)  # a segment of an older run
    assert not is_stale(segment_message(rid, 1, segment), last)
