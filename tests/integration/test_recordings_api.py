import asyncio
import io
import os
import wave
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.infrastructure.db.models import OutboxEventModel, RecordingModel
from tests.integration.helpers import API, auth_headers, missing_tool

pytestmark = pytest.mark.integration

requires_ffprobe = pytest.mark.skipif(
    missing_tool("ffprobe"), reason="ffprobe is not installed (brew install ffmpeg)"
)


def make_wav(seconds: int = 2, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * rate * seconds)
    return buffer.getvalue()


async def _project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(f"{API}/projects", json={"name": "Demo"}, headers=headers)
    return str(resp.json()["id"])


async def _create_recording(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    project_id: str,
    size: int,
    content_type: str = "audio/wav",
) -> httpx.Response:
    return await client.post(
        f"{API}/recordings",
        json={
            "project_id": project_id,
            "title": "Standup",
            "filename": "standup.wav",
            "content_type": content_type,
            "size_bytes": size,
        },
        headers=headers,
    )


async def _upload(url: str, data: bytes) -> None:
    async with httpx.AsyncClient() as http:
        resp = await http.put(url, content=data)
    assert resp.status_code == 200, resp.text


async def _recording_with_upload(
    client: httpx.AsyncClient, headers: dict[str, str], data: bytes
) -> str:
    project_id = await _project(client, headers)
    resp = await _create_recording(client, headers, project_id, len(data))
    assert resp.status_code == 201
    body = resp.json()
    assert body["recording"]["status"] == "created"
    await _upload(body["upload_url"], data)
    return str(body["recording"]["id"])


@requires_ffprobe
async def test_full_upload_flow(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    recording_id = await _recording_with_upload(api_client, headers, make_wav(seconds=2))

    url = f"{API}/recordings/{recording_id}/complete-upload"
    resp = await api_client.post(url, headers=headers)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "processing"
    assert body["duration_sec"] == pytest.approx(2.0, abs=0.1)

    # Idempotent: repeating the call changes nothing
    again = await api_client.post(url, headers=headers)
    assert again.status_code == 202
    assert again.json()["status"] == "processing"

    resp = await api_client.get(f"{API}/recordings/{recording_id}", headers=headers)
    assert resp.json()["status"] == "processing"

    resp = await api_client.get(f"{API}/recordings?status=processing", headers=headers)
    assert [r["id"] for r in resp.json()["items"]] == [recording_id]


@requires_ffprobe
async def test_concurrent_complete_upload_is_safe(
    api_client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = await auth_headers(api_client)
    recording_id = await _recording_with_upload(api_client, headers, make_wav())
    url = f"{API}/recordings/{recording_id}/complete-upload"

    first, second = await asyncio.gather(
        api_client.post(url, headers=headers), api_client.post(url, headers=headers)
    )
    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == second.json()["status"] == "processing"

    async with session_factory() as session:
        run = await session.scalar(
            select(RecordingModel.run).where(RecordingModel.id == UUID(recording_id))
        )
        requested = await session.scalar(
            select(func.count())
            .select_from(OutboxEventModel)
            .where(
                OutboxEventModel.aggregate_id == UUID(recording_id),
                OutboxEventModel.event_type == "recording.processing_requested",
            )
        )
    assert (run, requested) == (1, 1)  # one run and one event, not two
    me = await api_client.get(f"{API}/users/me", headers=headers)
    assert me.json()["minutes_used"] == 1  # charged once


async def test_complete_before_upload_keeps_state(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    project_id = await _project(api_client, headers)
    created = (await _create_recording(api_client, headers, project_id, 1024)).json()
    recording_id = created["recording"]["id"]

    resp = await api_client.post(
        f"{API}/recordings/{recording_id}/complete-upload", headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "UPLOAD_MISSING"

    resp = await api_client.get(f"{API}/recordings/{recording_id}", headers=headers)
    assert resp.json()["status"] == "created"


@requires_ffprobe
async def test_garbage_file_fails_recording(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    recording_id = await _recording_with_upload(api_client, headers, os.urandom(4096))

    resp = await api_client.post(
        f"{API}/recordings/{recording_id}/complete-upload", headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_MEDIA"

    resp = await api_client.get(f"{API}/recordings/{recording_id}", headers=headers)
    assert resp.json()["status"] == "failed"


async def test_size_and_type_limits(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    project_id = await _project(api_client, headers)

    too_big = await _create_recording(api_client, headers, project_id, 600 * 1024 * 1024)
    assert too_big.status_code == 413
    assert too_big.json()["code"] == "FILE_TOO_LARGE"

    bad_type = await _create_recording(api_client, headers, project_id, 1024, "application/pdf")
    assert bad_type.status_code == 415
    assert bad_type.json()["code"] == "UNSUPPORTED_MEDIA"

    no_project = await _create_recording(
        api_client, headers, "00000000-0000-0000-0000-000000000000", 1024
    )
    assert no_project.status_code == 404


async def test_recordings_are_private_and_deletable(api_client: httpx.AsyncClient) -> None:
    owner = await auth_headers(api_client)
    stranger = await auth_headers(api_client)
    project_id = await _project(api_client, owner)
    created = (await _create_recording(api_client, owner, project_id, 1024)).json()
    recording_id = created["recording"]["id"]

    assert (
        await api_client.get(f"{API}/recordings/{recording_id}", headers=stranger)
    ).status_code == 404
    assert (
        await api_client.post(f"{API}/recordings/{recording_id}/complete-upload", headers=stranger)
    ).status_code == 404
    assert (
        await api_client.delete(f"{API}/recordings/{recording_id}", headers=stranger)
    ).status_code == 404

    assert (
        await api_client.delete(f"{API}/recordings/{recording_id}", headers=owner)
    ).status_code == 204
    assert (
        await api_client.get(f"{API}/recordings/{recording_id}", headers=owner)
    ).status_code == 404


@requires_ffprobe
async def test_cannot_delete_while_processing(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    recording_id = await _recording_with_upload(api_client, headers, make_wav())
    await api_client.post(f"{API}/recordings/{recording_id}/complete-upload", headers=headers)

    resp = await api_client.delete(f"{API}/recordings/{recording_id}", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVALID_STATE"
