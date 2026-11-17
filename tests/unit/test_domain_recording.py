from typing import Any
from uuid import uuid4

import pytest

from scribe.domain.errors import (
    DomainValidationError,
    FileTooLargeError,
    InvalidStateError,
    UnsupportedMediaError,
)
from scribe.domain.events import (
    RecordingCompleted,
    RecordingCreated,
    RecordingDeleted,
    RecordingFailed,
    RecordingProcessingRequested,
    RecordingUploaded,
)
from scribe.domain.recordings import Recording, RecordingStatus, safe_filename
from scribe.domain.users import Plan

PLAN = Plan(id=1, name="free", monthly_minutes=300, max_file_mb=500)


def make_recording(**overrides: Any) -> Recording:
    params: dict[str, Any] = {
        "project_id": uuid4(),
        "owner_id": uuid4(),
        "title": "Weekly sync",
        "filename": "sync.mp3",
        "content_type": "audio/mpeg",
        "size_bytes": 1024,
        "language": "auto",
        "plan": PLAN,
    }
    params.update(overrides)
    return Recording.create(**params)


def recording_in(status: RecordingStatus) -> Recording:
    recording = make_recording()
    recording.status = status
    recording.pull_events()
    return recording


def test_create_sets_defaults_and_emits_event() -> None:
    recording = make_recording(title="  Weekly sync  ", language="UK")
    assert recording.status is RecordingStatus.CREATED
    assert recording.title == "Weekly sync"
    assert recording.language == "uk"
    assert [type(e) for e in recording.pull_events()] == [RecordingCreated]
    assert recording.pull_events() == []


def test_storage_key_is_sanitized() -> None:
    recording = make_recording(filename="../../etc/pa ss(1).mp3")
    prefix = f"{recording.owner_id}/{recording.id}/source/"
    assert recording.storage_key.startswith(prefix)
    filename = recording.storage_key.removeprefix(prefix)
    assert "/" not in filename and " " not in filename and not filename.startswith(".")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("a/b/talk.mp3", "talk.mp3"), ("..\\..\\x.wav", "x.wav"), ("", "source"), ("...", "source")],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"content_type": "application/pdf"}, UnsupportedMediaError),
        ({"size_bytes": 501 * 1024 * 1024}, FileTooLargeError),
        ({"size_bytes": 0}, DomainValidationError),
        ({"language": "ukr"}, DomainValidationError),
        ({"title": "   "}, DomainValidationError),
        ({"title": "x" * 201}, DomainValidationError),
        ({"language": "zz"}, DomainValidationError),
    ],
)
def test_create_validation(overrides: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        make_recording(**overrides)


def test_happy_path_and_events() -> None:
    recording = make_recording()
    recording.mark_uploaded()
    recording.start_processing(duration_sec=42.5)
    recording.complete()

    assert recording.status is RecordingStatus.COMPLETED
    assert recording.duration_sec == 42.5
    assert [type(e) for e in recording.pull_events()] == [
        RecordingCreated,
        RecordingUploaded,
        RecordingProcessingRequested,
        RecordingCompleted,
    ]


def test_fail_then_retry() -> None:
    recording = recording_in(RecordingStatus.PROCESSING)
    recording.fail("boom")
    assert recording.status is RecordingStatus.FAILED
    recording.retry()
    assert recording.status is RecordingStatus.PROCESSING
    events = recording.pull_events()
    assert [type(e) for e in events] == [RecordingFailed, RecordingProcessingRequested]


def test_cancel_then_retry() -> None:
    recording = recording_in(RecordingStatus.PROCESSING)
    recording.cancel()
    recording.retry()
    assert recording.status is RecordingStatus.PROCESSING


@pytest.mark.parametrize(
    ("status", "action"),
    [
        (RecordingStatus.CREATED, "complete"),
        (RecordingStatus.CREATED, "cancel"),
        (RecordingStatus.CREATED, "retry"),
        (RecordingStatus.CREATED, "fail"),
        (RecordingStatus.PROCESSING, "delete"),
        (RecordingStatus.PROCESSING, "mark_uploaded"),
        (RecordingStatus.COMPLETED, "cancel"),
        (RecordingStatus.COMPLETED, "retry"),
        (RecordingStatus.DELETED, "delete"),
        (RecordingStatus.DELETED, "retry"),
    ],
)
def test_illegal_transitions_raise(status: RecordingStatus, action: str) -> None:
    recording = recording_in(status)
    method = getattr(recording, action)
    with pytest.raises(InvalidStateError):
        method("reason") if action == "fail" else method()
    assert recording.status is status


def test_start_processing_requires_uploaded() -> None:
    with pytest.raises(InvalidStateError):
        recording_in(RecordingStatus.FAILED).start_processing(1.0)


@pytest.mark.parametrize(
    "status", [RecordingStatus.CREATED, RecordingStatus.COMPLETED, RecordingStatus.FAILED]
)
def test_delete_is_allowed_from(status: RecordingStatus) -> None:
    recording = recording_in(status)
    recording.delete()
    assert recording.status is RecordingStatus.DELETED
    assert [type(e) for e in recording.pull_events()] == [RecordingDeleted]
