import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from tests.e2e.helpers import (
    API,
    BASE_URL,
    STAGES,
    TERMINAL,
    create_project,
    create_recording,
    make_wav,
    parse_srt,
    parse_vtt,
    process_recording,
    register,
    upload,
    wait_for_status,
)

pytestmark = pytest.mark.e2e

AC3_SECONDS = float(os.environ.get("E2E_AC3_SECONDS", "1.0"))
JAEGER_URL = os.environ.get("JAEGER_URL", "http://localhost:16686").rstrip("/")
TRACE_SERVICES = {"traefik", "scribe-api", "scribe-relay", "scribe-consumer", "scribe-worker"}


@dataclass
class Journey:
    headers: dict[str, str]
    project_id: str
    recording_id: str
    trace_id: str
    duration: float
    first_event_latency: float
    messages: list[dict[str, Any]]


def _ws_url(recording_id: str, headers: dict[str, str]) -> str:
    token = headers["Authorization"].removeprefix("Bearer ")
    return BASE_URL.replace("http", "ws", 1) + f"/ws/recordings/{recording_id}?token={token}"


@pytest.fixture(scope="module")
async def journey(client: httpx.AsyncClient) -> Journey:
    """AC-1..AC-4: one user uploads a file and watches it being processed."""
    headers = await register(client)
    project_id = await create_project(client, headers)
    data = make_wav(seconds=6)
    created = await create_recording(client, headers, project_id, data)
    recording_id = str(created["recording"]["id"])
    await upload(created["upload_url"], data)  # the API did not touch the bytes (AC-2)

    messages: list[dict[str, Any]] = []
    latency: float | None = None
    async with connect(_ws_url(recording_id, headers), open_timeout=10) as ws:
        messages.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=10)))
        resp = await client.post(
            f"{API}/recordings/{recording_id}/complete-upload", headers=headers
        )
        assert resp.status_code == 202, resp.text
        accepted = time.monotonic()
        async with asyncio.timeout(120):
            async for raw in ws:
                message = json.loads(raw)
                messages.append(message)
                is_event = message["type"] in {"status", "progress"}
                if latency is None and is_event and message.get("status") != "created":
                    latency = max(0.0, time.monotonic() - accepted)
                if message["type"] in TERMINAL:
                    break
    assert latency is not None, "no event arrived after complete-upload"
    return Journey(
        headers=headers,
        project_id=project_id,
        recording_id=recording_id,
        trace_id=resp.headers["x-trace-id"],
        duration=float(resp.json()["duration_sec"]),
        first_event_latency=latency,
        messages=messages,
    )


async def test_the_stack_is_ready_and_internals_are_hidden(client: httpx.AsyncClient) -> None:
    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    assert {"postgres", "redis", "mongo", "minio"} <= set(ready.json()["checks"])

    assert (await client.get("/internal/auth")).status_code == 404
    assert (await client.get("/metrics")).status_code == 404
    unauthorized = await client.get(f"{API}/users/me")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["content-type"].startswith("application/problem+json")


async def test_the_first_event_arrives_quickly(journey: Journey) -> None:
    print(f"\nfirst event after complete-upload: {journey.first_event_latency:.3f}s")
    assert journey.first_event_latency <= AC3_SECONDS  # AC-3


async def test_the_websocket_reports_monotonic_progress_until_completion(
    journey: Journey,
) -> None:
    messages = journey.messages
    assert (messages[0]["type"], messages[0]["status"]) == ("status", "created")
    assert messages[-1]["type"] == "completed"
    progress = [m for m in messages if m["type"] == "progress"]
    overall = [m["overall"] for m in progress]
    assert overall == sorted(overall)
    assert overall[-1] == 100
    assert {m["stage"] for m in progress} == set(STAGES)
    assert any(m["type"] == "segment" for m in messages)


async def test_transcript_summary_and_search_agree(
    client: httpx.AsyncClient, journey: Journey
) -> None:
    base = f"{API}/recordings/{journey.recording_id}"
    page = (await client.get(f"{base}/transcript?limit=100", headers=journey.headers)).json()
    items = page["items"]
    assert items and page["language"]
    assert [i["start"] for i in items] == sorted(i["start"] for i in items)
    assert all(i["end"] > i["start"] for i in items)
    assert abs(items[-1]["end"] - journey.duration) < 1.0

    summary = (await client.get(f"{base}/summary", headers=journey.headers)).json()
    assert summary["summary"] and summary["topics"]

    found = (await client.get(f"{API}/search?q=stub", headers=journey.headers)).json()["items"]
    hits = {
        (h["segment"]["idx"], h["segment"]["start"], h["segment"]["end"])
        for h in found
        if h["recording_id"] == journey.recording_id
    }
    assert hits == {(i["idx"], i["start"], i["end"]) for i in items}  # AC-5: the same timecodes
    assert all("stub" in h["snippet"].lower() for h in found)


@pytest.mark.parametrize("fmt", ["txt", "srt", "vtt", "json"])
async def test_exports_are_valid_files(
    client: httpx.AsyncClient, journey: Journey, fmt: str
) -> None:
    base = f"{API}/recordings/{journey.recording_id}"
    items = (await client.get(f"{base}/transcript?limit=100", headers=journey.headers)).json()[
        "items"
    ]

    created = await client.post(f"{base}/exports", json={"format": fmt}, headers=journey.headers)
    assert created.status_code == 202
    link = (await client.get(f"{base}/exports/{fmt}", headers=journey.headers)).json()
    async with httpx.AsyncClient() as raw:
        downloaded = await raw.get(link["url"])
    assert downloaded.status_code == 200
    text = downloaded.text

    if fmt in {"srt", "vtt"}:
        cues = (parse_srt if fmt == "srt" else parse_vtt)(text)
        assert len(cues) == len(items)
        for (start, end, cue_text), item in zip(cues, items, strict=True):
            assert abs(start - item["start"]) < 0.002 and abs(end - item["end"]) < 0.002
            assert cue_text == item["text"]
    elif fmt == "txt":
        lines = text.strip().split("\n")
        assert len(lines) == len(items)
        assert all(re.fullmatch(r"\[\d{2}:\d{2}:\d{2}\] .+", line) for line in lines)
    else:
        data = json.loads(text)
        assert data["recording_id"] == journey.recording_id
        assert [s["text"] for s in data["segments"]] == [i["text"] for i in items]


async def test_one_trace_spans_every_service(journey: Journey) -> None:
    """AC-12: the trace id of the HTTP request is the trace id of every later step."""
    services: set[str] = set()
    async with httpx.AsyncClient(timeout=10) as raw:
        try:
            await raw.get(f"{JAEGER_URL}/api/services")
        except httpx.HTTPError:
            pytest.skip(f"Jaeger is not reachable at {JAEGER_URL}")
        deadline = time.monotonic() + 60  # spans are exported in batches
        while time.monotonic() < deadline:
            resp = await raw.get(f"{JAEGER_URL}/api/traces/{journey.trace_id}")
            data = resp.json().get("data") if resp.status_code == 200 else None
            if data:
                services = {p["serviceName"] for p in data[0]["processes"].values()}
                if services >= TRACE_SERVICES:
                    return
            await asyncio.sleep(2)
    pytest.fail(f"trace {journey.trace_id} lacks {sorted(TRACE_SERVICES - services)}")


async def test_another_user_cannot_see_the_recording(
    client: httpx.AsyncClient, journey: Journey
) -> None:
    stranger = await register(client)
    base = f"{API}/recordings/{journey.recording_id}"
    for suffix in ("", "/transcript", "/summary", "/exports/txt"):
        assert (await client.get(f"{base}{suffix}", headers=stranger)).status_code == 404, suffix
    found = (await client.get(f"{API}/search?q=stub", headers=stranger)).json()["items"]
    assert found == []

    with pytest.raises(InvalidStatus) as info:
        async with connect(_ws_url(journey.recording_id, stranger)):
            pass
    assert info.value.response.status_code == 403


async def test_garbage_upload_fails_cleanly(client: httpx.AsyncClient) -> None:
    headers = await register(client)
    project_id = await create_project(client, headers)
    noise = os.urandom(4096)
    created = await create_recording(client, headers, project_id, noise, filename="noise.wav")
    recording_id = str(created["recording"]["id"])
    await upload(created["upload_url"], noise)

    resp = await client.post(f"{API}/recordings/{recording_id}/complete-upload", headers=headers)
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_MEDIA"
    body = await wait_for_status(client, headers, recording_id, {"failed"}, timeout=10)
    assert body["stages"] == []

    retry = await client.post(f"{API}/recordings/{recording_id}/retry", headers=headers)
    assert retry.status_code == 409  # a rejected upload cannot skip its checks through retry
    assert retry.json()["code"] == "INVALID_STATE"


async def test_a_deleted_recording_disappears(client: httpx.AsyncClient) -> None:
    headers = await register(client)
    project_id = await create_project(client, headers)
    recording_id = await process_recording(client, headers, project_id)
    found = (await client.get(f"{API}/search?q=stub", headers=headers)).json()["items"]
    assert any(h["recording_id"] == recording_id for h in found)

    assert (
        await client.delete(f"{API}/recordings/{recording_id}", headers=headers)
    ).status_code == 204
    base = f"{API}/recordings/{recording_id}"
    assert (await client.get(base, headers=headers)).status_code == 404
    assert (await client.get(f"{base}/transcript", headers=headers)).status_code == 404
    found = (await client.get(f"{API}/search?q=stub", headers=headers)).json()["items"]
    assert all(h["recording_id"] != recording_id for h in found)
