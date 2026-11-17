from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from dependency_injector import providers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.application.event_handlers import RecordingCleanupHandler
from scribe.application.inbox import InboundEvent, InboxGuard
from scribe.config import Settings
from scribe.container import Container
from scribe.domain.exports import ExportFormat, export_key
from scribe.domain.pipeline import artifact_key
from scribe.domain.transcript import Segment, Summary, Transcription
from scribe.infrastructure.db.models import OutboxEventModel, RecordingModel
from tests.integration.helpers import API, auth_headers
from tests.integration.test_recordings_api import _create_recording, _project

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
BUCKETS = ["uploads", "artifacts", "exports"]


@pytest.fixture
async def worker(app_settings: Settings) -> AsyncIterator[Container]:
    container = Container()
    container.settings.override(providers.Object(app_settings))
    yield container
    await container.engine().dispose()
    await container.mongo().close()


async def _seed(container: Container, owner: UUID, recording: UUID) -> list[tuple[str, str]]:
    keys = [
        ("uploads", f"{owner}/{recording}/source/standup.wav"),
        ("artifacts", artifact_key(owner, recording, "audio/extracted.flac")),
        ("artifacts", artifact_key(owner, recording, "chunks/chunk_0000.wav")),
        ("exports", export_key(owner, recording, ExportFormat.SRT)),
    ]
    storage = container.object_storage()
    for bucket, key in keys:
        await storage.put_bytes(bucket, key, b"data", "application/octet-stream")
    await container.transcript_index().index(
        recording_id=recording,
        owner_id=owner,
        transcription=Transcription("en", "m", [Segment(0.0, 1.0, "hello")]),
        summary=Summary("short", ["topic"], [], "m"),
    )
    return keys


async def test_deleting_a_recording_purges_files_documents_and_the_row(
    api_client: httpx.AsyncClient, worker: Container, session_factory: Sessions
) -> None:
    headers = await auth_headers(api_client)
    owner = UUID((await api_client.get(f"{API}/users/me", headers=headers)).json()["id"])
    project_id = await _project(api_client, headers)
    target = UUID(
        (await _create_recording(api_client, headers, project_id, 1024)).json()["recording"]["id"]
    )
    other = UUID(
        (await _create_recording(api_client, headers, project_id, 1024)).json()["recording"]["id"]
    )
    target_keys = await _seed(worker, owner, target)
    other_keys = await _seed(worker, owner, other)

    resp = await api_client.delete(f"{API}/recordings/{target}", headers=headers)
    assert resp.status_code == 204

    async with session_factory() as session:
        stmt = select(OutboxEventModel).where(
            OutboxEventModel.aggregate_id == target,
            OutboxEventModel.event_type == "recording.deleted",
        )
        row = (await session.execute(stmt)).scalar_one()
    event = InboundEvent(
        event_id=row.event_id, event_type=row.event_type, payload=row.payload, headers=row.headers
    )

    handler = RecordingCleanupHandler(worker.object_storage(), worker.transcript_index(), BUCKETS)
    guard = InboxGuard(worker.uow, "recording.cleanup", handler)
    assert await guard.process(event) is True
    assert await guard.process(event) is False  # a duplicate delivery changes nothing

    storage = worker.object_storage()
    for bucket, key in target_keys:
        assert await storage.stat(bucket, key) is None
    for bucket, key in other_keys:  # a sibling recording of the same user is untouched
        assert await storage.stat(bucket, key) is not None

    db = worker.mongo_db()
    for name in ("transcript_segments", "transcripts", "summaries"):
        assert await db[name].count_documents({"recording_id": str(target)}) == 0
        assert await db[name].count_documents({"recording_id": str(other)}) == 1

    async with session_factory() as session:
        stmt = select(RecordingModel.id).where(RecordingModel.id.in_([target, other]))
        assert (await session.execute(stmt)).scalars().all() == [other]
