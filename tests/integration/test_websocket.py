import asyncio
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from dependency_injector import providers
from redis.asyncio import Redis
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedOK, InvalidStatus

from scribe.application.progress import (
    completed_message,
    failed_message,
    progress_message,
    segment_message,
    status_message,
)
from scribe.config import Settings
from scribe.container import Container
from scribe.domain.pipeline import StageName
from scribe.domain.transcript import Segment
from scribe.infrastructure.progress_bus import RedisProgressBus
from scribe.presentation.app import create_app
from tests.integration.helpers import API, auth_headers
from tests.integration.test_recordings_api import (
    _create_recording,
    _project,
    _recording_with_upload,
    make_wav,
    requires_ffprobe,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class LiveServer:
    http_url: str
    ws_url: str


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def live_server(app_settings: Settings) -> AsyncIterator[LiveServer]:
    container = Container()
    container.settings.override(providers.Object(app_settings))
    port = _free_port()
    config = uvicorn.Config(create_app(container), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        if task.done():
            task.result()  # surfaces a startup error
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("The test server did not start in time")
    yield LiveServer(http_url=f"http://127.0.0.1:{port}", ws_url=f"ws://127.0.0.1:{port}")
    server.should_exit = True
    await task


@pytest.fixture
async def bus(redis_url: str) -> AsyncIterator[RedisProgressBus]:
    """Plays the role of a worker process: its own Redis client, no shared memory with the API."""
    redis = Redis.from_url(redis_url, decode_responses=True)
    yield RedisProgressBus(redis)
    await redis.aclose()


async def _setup(live: LiveServer) -> tuple[str, UUID]:
    """A new user with a recording; returns the access token and the recording id."""
    async with httpx.AsyncClient(base_url=live.http_url) as client:
        headers = await auth_headers(client)
        project_id = await _project(client, headers)
        created = await _create_recording(client, headers, project_id, 1024)
        recording_id = UUID(created.json()["recording"]["id"])
    return headers["Authorization"].removeprefix("Bearer "), recording_id


def _url(live: LiveServer, recording_id: UUID, token: str) -> str:
    return f"{live.ws_url}/ws/recordings/{recording_id}?token={token}"


async def _recv(ws: ClientConnection) -> dict[str, Any]:
    return dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=5)))


async def _handshake_status(url: str) -> int:
    with pytest.raises(InvalidStatus) as info:
        async with connect(url, open_timeout=5):
            pass
    return int(info.value.response.status_code)


async def test_connection_requires_a_valid_token_and_ownership(live_server: LiveServer) -> None:
    token, recording_id = await _setup(live_server)
    stranger, _ = await _setup(live_server)
    base = f"{live_server.ws_url}/ws/recordings/{recording_id}"

    for url in (
        base,
        f"{base}?token=garbage",
        f"{base}?token={stranger}",  # another user's recording
        f"{live_server.ws_url}/ws/recordings/{uuid4()}?token={token}",  # unknown recording
    ):
        assert await _handshake_status(url) == 403


async def test_first_message_is_a_snapshot_from_the_database(live_server: LiveServer) -> None:
    token, recording_id = await _setup(live_server)
    async with connect(_url(live_server, recording_id, token)) as ws:
        first = await _recv(ws)
    assert first["type"] == "status"
    assert first["status"] == "created"
    assert first["recording_id"] == str(recording_id)


async def test_events_are_streamed_and_the_connection_closes_after_completion(
    live_server: LiveServer, bus: RedisProgressBus
) -> None:
    token, rid = await _setup(live_server)
    async with connect(_url(live_server, rid, token)) as ws:
        await _recv(ws)  # snapshot; the subscription is active from this point on

        await bus.publish_state(rid, progress_message(rid, 1, StageName.TRANSCRIBE, 42))
        await bus.publish_transient(rid, segment_message(rid, 1, Segment(12.4, 15.1, "hello")))
        await bus.publish_state(rid, completed_message(rid, 1))

        progress = await _recv(ws)
        segment = await _recv(ws)
        done = await _recv(ws)

        assert (progress["type"], progress["stage"], progress["percent"]) == (
            "progress",
            "transcribe",
            42,
        )
        assert progress["overall"] == 40
        assert (segment["type"], segment["start"], segment["text"]) == ("segment", 12.4, "hello")
        assert done["type"] == "completed"
        with pytest.raises(ConnectionClosedOK):
            await asyncio.wait_for(ws.recv(), timeout=5)


@requires_ffprobe
async def test_complete_upload_notifies_websocket_without_relay(
    live_server: LiveServer, bus: RedisProgressBus
) -> None:
    async with httpx.AsyncClient(base_url=live_server.http_url) as client:
        headers = await auth_headers(client)
        rid = UUID(await _recording_with_upload(client, headers, make_wav()))
        token = headers["Authorization"].removeprefix("Bearer ")
        async with connect(_url(live_server, rid, token)) as ws:
            assert (await _recv(ws))["status"] == "created"
            response = await client.post(f"{API}/recordings/{rid}/complete-upload", headers=headers)
            assert response.status_code == 202
            processing = status_message(rid, 1, "processing")
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
            assert first == processing

            # Simulate the consumer later delivering the same status from the outbox.
            await bus.publish_state(rid, processing)
            completed = completed_message(rid, 1)
            await bus.publish_state(rid, completed)
            assert await _recv(ws) == completed


async def test_reconnecting_client_receives_the_last_stored_state(
    live_server: LiveServer, bus: RedisProgressBus
) -> None:
    token, rid = await _setup(live_server)
    await bus.publish_state(rid, progress_message(rid, 1, StageName.TRANSCRIBE, 42))

    async with connect(_url(live_server, rid, token)) as ws:
        first = await _recv(ws)
        assert (first["type"], first["stage"], first["percent"]) == ("progress", "transcribe", 42)

        await bus.publish_state(rid, progress_message(rid, 1, StageName.TRANSCRIBE, 50))
        assert (await _recv(ws))["percent"] == 50


async def test_a_finished_recording_sends_its_state_and_closes(
    live_server: LiveServer, bus: RedisProgressBus
) -> None:
    token, rid = await _setup(live_server)
    await bus.publish_state(rid, completed_message(rid, 1))

    async with connect(_url(live_server, rid, token)) as ws:
        assert (await _recv(ws))["type"] == "completed"
        with pytest.raises(ConnectionClosedOK):
            await asyncio.wait_for(ws.recv(), timeout=5)


async def test_every_connected_client_receives_the_events(
    live_server: LiveServer, bus: RedisProgressBus
) -> None:
    token, rid = await _setup(live_server)
    async with (
        connect(_url(live_server, rid, token)) as first,
        connect(_url(live_server, rid, token)) as second,
    ):
        await _recv(first)
        await _recv(second)

        await bus.publish_state(rid, progress_message(rid, 1, StageName.EXTRACT_AUDIO, 30))

        assert (await _recv(first))["percent"] == 30
        assert (await _recv(second))["percent"] == 30


async def test_stored_state_never_goes_backwards(bus: RedisProgressBus) -> None:
    rid = uuid4()

    async def stored() -> dict[str, Any]:
        async with bus.subscribe(rid) as subscription:
            return await subscription.last_state() or {}

    await bus.publish_state(rid, status_message(rid, 1, "processing"))
    await bus.publish_state(rid, progress_message(rid, 1, StageName.INDEX, 100))
    await bus.publish_state(rid, status_message(rid, 1, "processing"))  # late, lower rank
    assert (await stored())["type"] == "progress"

    await bus.publish_state(rid, completed_message(rid, 1))
    await bus.publish_state(rid, progress_message(rid, 1, StageName.INDEX, 100))  # late
    assert (await stored())["type"] == "completed"

    await bus.publish_state(rid, status_message(rid, 2, "processing"))  # retry: a new run wins
    assert (await stored())["run"] == 2


async def test_progress_within_a_run_is_monotonic_but_a_stage_retry_may_restart(
    bus: RedisProgressBus,
) -> None:
    rid = uuid4()

    async def stored() -> dict[str, Any]:
        async with bus.subscribe(rid) as subscription:
            return await subscription.last_state() or {}

    await bus.publish_state(rid, progress_message(rid, 1, StageName.TRANSCRIBE, 90))
    await bus.publish_state(rid, progress_message(rid, 1, StageName.TRANSCRIBE, 10))  # late
    assert (await stored())["percent"] == 90

    await bus.publish_state(rid, progress_message(rid, 1, StageName.EXTRACT_AUDIO, 100))  # earlier
    assert (await stored())["stage"] == "transcribe"

    await bus.publish_state(rid, progress_message(rid, 1, StageName.TRANSCRIBE, 0, attempt=2))
    state = await stored()
    assert (state["percent"], state["attempt"]) == (0, 2)  # a retried stage may restart

    await bus.publish_state(rid, progress_message(rid, 1, StageName.BUILD_SUBTITLES, 5))
    assert (await stored())["stage"] == "build_subtitles"

    await bus.publish_state(rid, failed_message(rid, 1, "build_subtitles", "boom"))
    await bus.publish_state(rid, completed_message(rid, 1))  # the first final state wins
    assert (await stored())["type"] == "failed"
