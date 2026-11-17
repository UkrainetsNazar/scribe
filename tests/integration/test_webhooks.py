import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import aio_pika
import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.application.event_handlers import WebhookDeliveryHandler
from scribe.application.inbox import InboxGuard
from scribe.domain.events import RecordingCompleted
from scribe.domain.webhooks import verify_signature
from scribe.infrastructure.db.uow import SqlAlchemyUnitOfWork
from scribe.infrastructure.messaging.consumer import RabbitMqConsumer
from scribe.infrastructure.messaging.publisher import RabbitMqPublisher
from scribe.infrastructure.messaging.relay import OutboxRelay
from scribe.infrastructure.messaging.topology import WEBHOOK_DELIVERY
from scribe.infrastructure.webhook_sender import HttpxWebhookSender
from tests.integration.conftest import RabbitBroker
from tests.integration.helpers import API, auth_headers
from tests.integration.test_messaging import _clean_state, _wait_for  # noqa: F401
from tests.integration.test_recordings_api import _create_recording, _project
from tests.integration.test_websocket import _free_port

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


async def test_webhook_configuration(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    project_id = await _project(api_client, headers)
    url = f"{API}/projects/{project_id}/webhook"

    assert (await api_client.get(url, headers=headers)).json() == {"url": None}

    resp = await api_client.put(url, json={"url": "https://example.com/hook"}, headers=headers)
    assert resp.status_code == 200
    secret = resp.json()["secret"]
    assert secret.startswith("whsec_")
    assert (await api_client.get(url, headers=headers)).json() == {
        "url": "https://example.com/hook"
    }

    again = await api_client.put(url, json={"url": "https://example.org/hook"}, headers=headers)
    assert again.json() == {"url": "https://example.org/hook", "secret": secret}  # kept

    rotated = await api_client.post(f"{url}/rotate-secret", headers=headers)
    assert rotated.status_code == 200 and rotated.json()["secret"] != secret

    assert (await api_client.delete(url, headers=headers)).status_code == 204
    assert (await api_client.get(url, headers=headers)).json() == {"url": None}
    conflict = await api_client.post(f"{url}/rotate-secret", headers=headers)
    assert conflict.status_code == 409


async def test_webhook_validation_and_privacy(api_client: httpx.AsyncClient) -> None:
    owner = await auth_headers(api_client)
    stranger = await auth_headers(api_client)
    project_id = await _project(api_client, owner)
    url = f"{API}/projects/{project_id}/webhook"

    for body in ({"url": "ftp://example.com/x"}, {"url": "https://u:p@example.com/x"}, {}):
        assert (await api_client.put(url, json=body, headers=owner)).status_code == 422

    assert (await api_client.get(url)).status_code == 401
    for resp in (
        await api_client.get(url, headers=stranger),
        await api_client.put(url, json={"url": "https://example.com"}, headers=stranger),
        await api_client.post(f"{url}/rotate-secret", headers=stranger),
        await api_client.delete(url, headers=stranger),
    ):
        assert resp.status_code == 404


@dataclass
class Receiver:
    url: str
    failures_left: int = 0
    requests: list[tuple[dict[str, str], bytes]] = field(default_factory=list)


@pytest.fixture
async def receiver() -> AsyncIterator[Receiver]:
    """A real HTTP endpoint that records what it receives; the first N calls can fail."""
    port = _free_port()
    state = Receiver(url=f"http://127.0.0.1:{port}/hook")

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        state.requests.append(({k.decode(): v.decode() for k, v in scope["headers"]}, body))
        status = 500 if state.failures_left > 0 else 200
        state.failures_left = max(0, state.failures_left - 1)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        if task.done():
            task.result()
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("The receiver did not start in time")
    yield state
    server.should_exit = True
    await task


async def _purge(url: str, *names: str) -> None:
    connection = await aio_pika.connect(url)
    async with connection:
        channel = await connection.channel()
        for name in names:
            queue = await channel.declare_queue(name, durable=True)
            await queue.purge()


async def test_completed_recording_is_delivered_signed_and_retried(
    api_client: httpx.AsyncClient,
    session_factory: Sessions,
    rabbit: RabbitBroker,
    receiver: Receiver,
) -> None:
    receiver.failures_left = 1  # the first attempt fails and must be retried
    headers = await auth_headers(api_client)
    owner = UUID((await api_client.get(f"{API}/users/me", headers=headers)).json()["id"])
    project_id = await _project(api_client, headers)
    configured = await api_client.put(
        f"{API}/projects/{project_id}/webhook", json={"url": receiver.url}, headers=headers
    )
    secret = configured.json()["secret"]
    recording_id = UUID(
        (await _create_recording(api_client, headers, project_id, 1024)).json()["recording"]["id"]
    )

    event = RecordingCompleted(recording_id=recording_id, owner_id=owner, run=1)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.add_events([event])
        await uow.commit()

    publisher = RabbitMqPublisher(rabbit.url, (1,), timeout_sec=5)
    await publisher.connect()  # declares the topology, including the webhook queue
    await _purge(rabbit.url, WEBHOOK_DELIVERY.name)
    sender = HttpxWebhookSender(timeout_sec=5, allow_private_networks=True)
    guard = InboxGuard(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        WEBHOOK_DELIVERY.name,
        WebhookDeliveryHandler(sender),
    )
    consumer = RabbitMqConsumer(rabbit.url, WEBHOOK_DELIVERY, guard, (1,), prefetch=5)
    await consumer.start()
    try:
        relay = OutboxRelay(session_factory, publisher, batch_size=1000, poll_interval_sec=0.05)
        await relay.run_once()
        await _wait_for(lambda: len(receiver.requests) >= 2, timeout=20)
    finally:
        await consumer.close()
        await publisher.close()

    (first_headers, _first_body), (second_headers, second_body) = receiver.requests[:2]
    assert (
        first_headers["x-scribe-delivery"]
        == second_headers["x-scribe-delivery"]
        == str(event.event_id)
    )
    for request_headers, body in receiver.requests[:2]:
        assert request_headers["x-scribe-event"] == "recording.completed"
        assert verify_signature(
            secret, request_headers["x-scribe-signature"], body, now=time.time()
        )
    payload = json.loads(second_body)
    assert payload["id"] == str(event.event_id)
    assert payload["type"] == "recording.completed"
    assert payload["data"]["recording_id"] == str(recording_id)
    assert (payload["data"]["title"], payload["data"]["status"]) == ("Standup", "completed")
