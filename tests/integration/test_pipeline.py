import asyncio
import io
import json
import shutil
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from minio import Minio
from pymongo import MongoClient
from redis import Redis as SyncRedis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import scribe.workers.pipeline_tasks  # noqa: F401  (registers the Celery tasks)
from scribe.domain.pipeline import PIPELINE_STAGES
from scribe.infrastructure.celery_app import PIPELINE_QUEUE, celery_app, stage_task_name
from tests.integration.conftest import MINIO_PASSWORD, MINIO_USER, MONGO_DB
from tests.integration.helpers import API, auth_headers
from tests.integration.test_recordings_api import _recording_with_upload, make_wav

pytestmark = pytest.mark.integration

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are not installed (brew install ffmpeg)",
)
SOURCE_NAME = "standup.wav"  # filename used by _recording_with_upload


def _minio(endpoint: str) -> Minio:
    return Minio(endpoint, access_key=MINIO_USER, secret_key=MINIO_PASSWORD, secure=False)


async def _run_pipeline(recording_id: str, owner_id: str, run: int) -> None:
    """Queues the first stage; in eager mode the whole chain runs inside this call."""

    def dispatch() -> None:
        celery_app.signature(
            stage_task_name(PIPELINE_STAGES[0]),
            args=[recording_id, owner_id, run],
            queue=PIPELINE_QUEUE,
        ).apply_async()

    await asyncio.to_thread(dispatch)  # tasks use asyncio.run(), so they need their own thread


async def _owner_id(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    return str((await client.get(f"{API}/users/me", headers=headers)).json()["id"])


async def _get(
    client: httpx.AsyncClient, headers: dict[str, str], recording_id: str
) -> dict[str, object]:
    resp = await client.get(f"{API}/recordings/{recording_id}", headers=headers)
    assert resp.status_code == 200
    return dict(resp.json())


@requires_ffmpeg
async def test_full_pipeline_completes(
    api_client: httpx.AsyncClient,
    pipeline_env: None,
    minio_endpoint: str,
    mongo_url: str,
    redis_url: str,
) -> None:
    headers = await auth_headers(api_client)
    owner_id = await _owner_id(api_client, headers)
    recording_id = await _recording_with_upload(api_client, headers, make_wav(seconds=3))

    resp = await api_client.post(
        f"{API}/recordings/{recording_id}/complete-upload", headers=headers
    )
    assert resp.status_code == 202
    assert [s["status"] for s in resp.json()["stages"]] == ["pending"] * len(PIPELINE_STAGES)

    await _run_pipeline(recording_id, owner_id, run=1)

    body = await _get(api_client, headers, recording_id)
    assert body["status"] == "completed"
    assert body["language"] == "en"  # detected (the stub reports English)
    assert [s["name"] for s in body["stages"]] == [s.value for s in PIPELINE_STAGES]  # type: ignore[attr-defined]
    assert all(s["status"] == "completed" and s["progress"] == 100 for s in body["stages"])  # type: ignore[attr-defined]

    me = await api_client.get(f"{API}/users/me", headers=headers)
    assert me.json()["minutes_used"] == 1

    prefix = f"{owner_id}/{recording_id}"
    srt = _minio(minio_endpoint).get_object("artifacts", f"{prefix}/subtitles/subtitles.srt")
    assert b"Stub transcript segment 1." in srt.read()

    def mongo_counts() -> tuple[int, int]:
        with MongoClient(mongo_url) as client:
            segments = client[MONGO_DB]["transcript_segments"]
            total = segments.count_documents({"recording_id": recording_id})
            found = segments.count_documents(
                {"recording_id": recording_id, "$text": {"$search": "transcript"}}
            )
            return total, found

    total, found = await asyncio.to_thread(mongo_counts)
    assert total >= 1
    assert found == total  # the text index finds every segment

    def stored_state() -> dict[str, object]:
        with SyncRedis.from_url(redis_url, decode_responses=True) as client:
            raw = client.get(f"progress:{recording_id}")
        return dict(json.loads(raw)) if raw else {}

    state = await asyncio.to_thread(stored_state)
    assert (state["type"], state["stage"], state["percent"], state["run"]) == (
        "progress",
        "index",
        100,
        1,
    )


@requires_ffmpeg
async def test_missing_source_fails_then_retry_succeeds(
    api_client: httpx.AsyncClient, pipeline_env: None, minio_endpoint: str
) -> None:
    headers = await auth_headers(api_client)
    owner_id = await _owner_id(api_client, headers)
    data = make_wav(seconds=2)
    recording_id = await _recording_with_upload(api_client, headers, data)
    await api_client.post(f"{API}/recordings/{recording_id}/complete-upload", headers=headers)

    # The source disappears after validation: a permanent error, no retries
    storage = _minio(minio_endpoint)
    source_key = f"{owner_id}/{recording_id}/source/{SOURCE_NAME}"
    storage.remove_object("uploads", source_key)
    await _run_pipeline(recording_id, owner_id, run=1)

    body = await _get(api_client, headers, recording_id)
    assert body["status"] == "failed"
    first_stage = body["stages"][0]  # type: ignore[index]
    assert first_stage["status"] == "failed" and "missing" in first_stage["error"]
    assert first_stage.get("attempts", True)

    # Restore the file and retry: processing resumes with a new run
    storage.put_object("uploads", source_key, io.BytesIO(data), len(data))
    resp = await api_client.post(f"{API}/recordings/{recording_id}/retry", headers=headers)
    assert resp.status_code == 202 and resp.json()["status"] == "processing"
    await _run_pipeline(recording_id, owner_id, run=1)  # a stale run is ignored
    assert (await _get(api_client, headers, recording_id))["status"] == "processing"

    await _run_pipeline(recording_id, owner_id, run=2)
    assert (await _get(api_client, headers, recording_id))["status"] == "completed"
    me = await api_client.get(f"{API}/users/me", headers=headers)
    assert me.json()["minutes_used"] == 1  # retry does not charge again


@requires_ffmpeg
async def test_cancel_and_retry_endpoints(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    recording_id = await _recording_with_upload(api_client, headers, make_wav())
    await api_client.post(f"{API}/recordings/{recording_id}/complete-upload", headers=headers)
    base = f"{API}/recordings/{recording_id}"

    for _ in range(2):  # idempotent
        resp = await api_client.post(f"{base}/cancel", headers=headers)
        assert resp.status_code == 202 and resp.json()["status"] == "cancelled"
    for _ in range(2):  # idempotent
        resp = await api_client.post(f"{base}/retry", headers=headers)
        assert resp.status_code == 202 and resp.json()["status"] == "processing"
    assert all(s["status"] == "pending" for s in resp.json()["stages"])


async def test_retry_and_cancel_conflict_in_wrong_state(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    project = await api_client.post(f"{API}/projects", json={"name": "P"}, headers=headers)
    created = await api_client.post(
        f"{API}/recordings",
        json={
            "project_id": project.json()["id"],
            "title": "T",
            "filename": "a.wav",
            "content_type": "audio/wav",
            "size_bytes": 1024,
        },
        headers=headers,
    )
    base = f"{API}/recordings/{created.json()['recording']['id']}"
    for action in ("retry", "cancel"):
        resp = await api_client.post(f"{base}/{action}", headers=headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == "INVALID_STATE"


@requires_ffmpeg
async def test_quota_exceeded_is_rejected_before_processing(
    api_client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = await auth_headers(api_client)
    owner_id = await _owner_id(api_client, headers)
    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO usage (user_id, period, minutes_used) VALUES (:u, :p, 300)"),
            {"u": uuid.UUID(owner_id), "p": datetime.now(UTC).strftime("%Y-%m")},
        )
        await session.commit()
    recording_id = await _recording_with_upload(api_client, headers, make_wav())

    resp = await api_client.post(
        f"{API}/recordings/{recording_id}/complete-upload", headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "QUOTA_EXCEEDED"
    body = await _get(api_client, headers, recording_id)
    assert body["status"] == "failed"
    assert body["stages"] == []
