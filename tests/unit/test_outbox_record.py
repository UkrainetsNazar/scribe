import json
from uuid import uuid4

from scribe.application.outbox import to_outbox_record
from scribe.domain.events import RecordingFailed, RecordingProcessingRequested


def test_record_describes_the_event() -> None:
    recording_id, owner_id = uuid4(), uuid4()
    event = RecordingProcessingRequested(
        recording_id=recording_id, owner_id=owner_id, storage_key="u/r/source/a.wav"
    )
    record = to_outbox_record(event, {"trace_id": "abc"})

    assert record.event_id == event.event_id
    assert record.event_type == "recording.processing_requested"
    assert record.aggregate_type == "recording"
    assert record.aggregate_id == recording_id
    assert record.headers == {"trace_id": "abc"}
    assert record.payload["recording_id"] == str(recording_id)
    assert record.payload["storage_key"] == "u/r/source/a.wav"
    json.dumps(record.payload)  # must be JSON-serializable (UUID and datetime are strings)


def test_each_event_type_has_a_unique_name() -> None:
    first = RecordingFailed(recording_id=uuid4(), owner_id=uuid4(), reason="x")
    second = RecordingProcessingRequested(recording_id=uuid4(), owner_id=uuid4(), storage_key="k")
    assert first.event_type != second.event_type
