import json
from uuid import uuid4

import pytest

from scribe.application.event_handlers import WebhookDeliveryHandler
from scribe.application.inbox import InboundEvent, PermanentHandlerError
from scribe.application.interfaces import UnitOfWorkFactory
from scribe.application.use_cases.webhooks import (
    ConfigureWebhook,
    DeleteWebhook,
    GetWebhook,
    RotateWebhookSecret,
)
from scribe.application.webhooks import WebhookDeliveryError
from scribe.domain.errors import DomainValidationError, NotFoundError
from scribe.domain.projects import Project
from scribe.domain.recordings import Recording, RecordingStatus
from tests.unit.fakes import FakeDb, FakeUnitOfWork, FakeWebhookSender
from tests.unit.test_domain_recording import make_recording

OWNER = uuid4()
URL = "https://example.com/hook"


@pytest.fixture
def project(db: FakeDb) -> Project:
    project = Project.create(OWNER, "Demo")
    db.projects[project.id] = project
    return project


async def test_configure_returns_a_secret_and_keeps_it_when_the_url_changes(
    uow_factory: UnitOfWorkFactory, project: Project
) -> None:
    configure = ConfigureWebhook(uow_factory)
    first = await configure.execute(OWNER, project.id, URL)
    assert first.url == URL and first.secret
    second = await configure.execute(OWNER, project.id, "https://example.org/hook")
    assert second.url == "https://example.org/hook"
    assert second.secret == first.secret


async def test_get_never_reveals_the_secret(
    uow_factory: UnitOfWorkFactory, project: Project
) -> None:
    await ConfigureWebhook(uow_factory).execute(OWNER, project.id, URL)
    config = await GetWebhook(uow_factory).execute(OWNER, project.id)
    assert (config.url, config.secret) == (URL, None)


async def test_rotate_and_delete(uow_factory: UnitOfWorkFactory, project: Project) -> None:
    first = await ConfigureWebhook(uow_factory).execute(OWNER, project.id, URL)
    rotated = await RotateWebhookSecret(uow_factory).execute(OWNER, project.id)
    assert rotated.secret and rotated.secret != first.secret

    await DeleteWebhook(uow_factory).execute(OWNER, project.id)
    assert (await GetWebhook(uow_factory).execute(OWNER, project.id)).url is None


async def test_invalid_url_and_foreign_project(
    uow_factory: UnitOfWorkFactory, project: Project
) -> None:
    with pytest.raises(DomainValidationError):
        await ConfigureWebhook(uow_factory).execute(OWNER, project.id, "ftp://example.com")
    with pytest.raises(NotFoundError):
        await GetWebhook(uow_factory).execute(uuid4(), project.id)


def _recording(db: FakeDb, project: Project, status: RecordingStatus) -> Recording:
    recording = make_recording(owner_id=OWNER, project_id=project.id, title="Weekly sync")
    recording.status = status
    recording.duration_sec = 12.5
    recording.pull_events()
    db.recordings[recording.id] = recording
    return recording


def _event(event_type: str, recording: Recording, **extra: object) -> InboundEvent:
    return InboundEvent(
        event_id=uuid4(),
        event_type=event_type,
        payload={
            "recording_id": str(recording.id),
            "owner_id": str(recording.owner_id),
            "run": 1,
            "occurred_at": "2026-01-01T00:00:00+00:00",
            **extra,
        },
        headers={},
    )


async def test_completed_event_is_delivered_with_recording_details(
    db: FakeDb, project: Project
) -> None:
    project.set_webhook(URL)
    recording = _recording(db, project, RecordingStatus.COMPLETED)
    event = _event("recording.completed", recording)
    sender = FakeWebhookSender()

    await WebhookDeliveryHandler(sender).handle(event, FakeUnitOfWork(db))

    (delivery,) = sender.sent
    assert (delivery.url, delivery.secret) == (URL, project.webhook_secret)
    assert (delivery.event_id, delivery.event_type) == (event.event_id, "recording.completed")
    body = json.loads(delivery.body)
    assert (body["id"], body["type"]) == (str(event.event_id), "recording.completed")
    assert body["data"] == {
        "recording_id": str(recording.id),
        "project_id": str(project.id),
        "title": "Weekly sync",
        "status": "completed",
        "run": 1,
        "duration_sec": 12.5,
        "language": recording.language,
    }


async def test_failed_event_carries_the_stage_and_the_error(db: FakeDb, project: Project) -> None:
    project.set_webhook(URL)
    recording = _recording(db, project, RecordingStatus.FAILED)
    event = _event("recording.failed", recording, stage="transcribe", reason="boom")
    sender = FakeWebhookSender()

    await WebhookDeliveryHandler(sender).handle(event, FakeUnitOfWork(db))

    data = json.loads(sender.sent[0].body)["data"]
    assert (data["status"], data["stage"], data["error"]) == ("failed", "transcribe", "boom")


async def test_nothing_is_sent_without_a_webhook_or_a_recording(
    db: FakeDb, project: Project
) -> None:
    sender = FakeWebhookSender()
    handler = WebhookDeliveryHandler(sender)

    recording = _recording(db, project, RecordingStatus.COMPLETED)
    await handler.handle(_event("recording.completed", recording), FakeUnitOfWork(db))  # no webhook

    project.set_webhook(URL)
    gone = _recording(db, project, RecordingStatus.DELETED)
    await handler.handle(_event("recording.completed", gone), FakeUnitOfWork(db))
    assert sender.sent == []


async def test_sender_errors_propagate_so_the_consumer_retries(
    db: FakeDb, project: Project
) -> None:
    project.set_webhook(URL)
    recording = _recording(db, project, RecordingStatus.COMPLETED)
    handler = WebhookDeliveryHandler(FakeWebhookSender(error=WebhookDeliveryError("boom")))
    with pytest.raises(WebhookDeliveryError):
        await handler.handle(_event("recording.completed", recording), FakeUnitOfWork(db))


async def test_malformed_event_is_a_permanent_error(db: FakeDb) -> None:
    broken = InboundEvent(
        event_id=uuid4(), event_type="recording.completed", payload={}, headers={}
    )
    with pytest.raises(PermanentHandlerError):
        await WebhookDeliveryHandler(FakeWebhookSender()).handle(broken, FakeUnitOfWork(db))
