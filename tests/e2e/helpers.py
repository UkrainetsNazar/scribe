import asyncio
import io
import os
import re
import time
import uuid
import wave
from typing import Any

import httpx

BASE_URL = os.environ.get("SCRIBE_BASE_URL", "http://localhost:8010").rstrip("/")
API = "/api/v1"
PASSWORD = "e2e-password-1"
TERMINAL = {"completed", "failed", "cancelled"}
STAGES = [
    "extract_audio",
    "normalize_and_chunk",
    "transcribe",
    "build_subtitles",
    "summarize",
    "index",
]


def make_wav(seconds: float = 6.0, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


async def _post_throttled(
    client: httpx.AsyncClient, path: str, body: dict[str, str]
) -> httpx.Response:
    """The auth routes have a per-IP limit; honour Retry-After instead of failing."""
    for _ in range(30):
        resp = await client.post(path, json=body)
        if resp.status_code != 429:
            return resp
        await asyncio.sleep(float(resp.headers.get("retry-after", "1")))
    raise AssertionError("still rate limited after 30 attempts")


async def register(client: httpx.AsyncClient) -> dict[str, str]:
    email = f"e2e-{uuid.uuid4().hex[:12]}@example.com"
    body = {"email": email, "password": PASSWORD}
    resp = await _post_throttled(client, f"{API}/auth/register", body)
    assert resp.status_code == 201, resp.text
    resp = await _post_throttled(client, f"{API}/auth/login", body)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def create_project(
    client: httpx.AsyncClient, headers: dict[str, str], name: str = "E2E"
) -> str:
    resp = await client.post(f"{API}/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def create_recording(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    project_id: str,
    data: bytes,
    *,
    filename: str = "e2e.wav",
    content_type: str = "audio/wav",
    title: str = "E2E recording",
) -> dict[str, Any]:
    resp = await client.post(
        f"{API}/recordings",
        json={
            "project_id": project_id,
            "title": title,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(data),
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def upload(url: str, data: bytes) -> None:
    async with httpx.AsyncClient(timeout=60) as raw:  # straight to object storage
        resp = await raw.put(url, content=data)
    assert resp.status_code == 200, resp.text


async def wait_for_status(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    recording_id: str,
    wanted: set[str],
    timeout: float = 120.0,  # noqa: ASYNC109
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        resp = await client.get(f"{API}/recordings/{recording_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        body = dict(resp.json())
        if body["status"] in wanted:
            return body
        assert time.monotonic() < deadline, f"still '{body['status']}' after {timeout}s"
        await asyncio.sleep(1)


async def process_recording(
    client: httpx.AsyncClient, headers: dict[str, str], project_id: str, seconds: float = 3.0
) -> str:
    """Creates, uploads, starts and waits until a recording is completed."""
    data = make_wav(seconds)
    created = await create_recording(client, headers, project_id, data)
    recording_id = str(created["recording"]["id"])
    await upload(created["upload_url"], data)
    resp = await client.post(f"{API}/recordings/{recording_id}/complete-upload", headers=headers)
    assert resp.status_code == 202, resp.text
    await wait_for_status(client, headers, recording_id, {"completed"})
    return recording_id


# ── Subtitle parsers (strict: a malformed file fails the test) ────────────────

_SRT_TIME = r"(\d{2}):(\d{2}):(\d{2}),(\d{3})"
_VTT_TIME = r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})"


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _cue_times(line: str, pattern: str) -> tuple[float, float]:
    match = re.fullmatch(f"{pattern} --> {pattern}", line)
    assert match, f"bad timing line: {line!r}"
    values = match.groups()
    start, end = _seconds(*values[:4]), _seconds(*values[4:])
    assert end > start, line
    return start, end


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    cues = []
    blocks = [b.strip("\n") for b in text.split("\n\n") if b.strip("\n")]
    for number, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        assert lines[0] == str(number), f"cue numbers must be sequential: {lines[0]!r}"
        start, end = _cue_times(lines[1], _SRT_TIME)
        assert lines[2:] and all(lines[2:]), "a cue needs text"
        cues.append((start, end, " ".join(lines[2:])))
    return cues


def parse_vtt(text: str) -> list[tuple[float, float, str]]:
    assert text.startswith("WEBVTT\n\n"), "a WebVTT file starts with the WEBVTT header"
    cues = []
    for block in (b.strip("\n") for b in text[len("WEBVTT\n\n") :].split("\n\n")):
        if not block:
            continue
        lines = block.split("\n")
        start, end = _cue_times(lines[0], _VTT_TIME)
        assert lines[1:] and all(lines[1:]), "a cue needs text"
        cues.append((start, end, " ".join(lines[1:])))
    return cues
