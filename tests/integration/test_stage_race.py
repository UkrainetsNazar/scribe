import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.application.interfaces import UnitOfWork
from scribe.application.pipeline import StageRunner
from scribe.application.use_cases.recordings import CancelRecording, RetryRecording
from scribe.domain.errors import InvalidStateError
from scribe.domain.pipeline import ProcessingStage, StageName, StageStatus
from scribe.domain.recordings import Recording, RecordingStatus
from scribe.infrastructure.db.uow import SqlAlchemyUnitOfWork
from tests.integration.helpers import API, auth_headers
from tests.integration.test_recordings_api import _create_recording, _project

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
STAGE = StageName.TRANSCRIBE


class PausingUnitOfWork(SqlAlchemyUnitOfWork):
    """Pauses at the first `stages.get`, which comes right after the recording has been read."""

    def __init__(self, sessions: Sessions, entered: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__(sessions)
        self._entered = entered
        self._release = release

    async def __aenter__(self) -> "PausingUnitOfWork":
        await super().__aenter__()
        original: Callable[[UUID, StageName], Awaitable[ProcessingStage | None]] = self.stages.get

        async def paused(recording_id: UUID, name: StageName) -> ProcessingStage | None:
            self._entered.set()
            await self._release.wait()
            return await original(recording_id, name)

        self.stages.get = paused  # type: ignore[method-assign]
        return self


async def _processing_recording(client: httpx.AsyncClient, sessions: Sessions) -> tuple[UUID, UUID]:
    headers = await auth_headers(client)
    owner = UUID((await client.get(f"{API}/users/me", headers=headers)).json()["id"])
    project_id = await _project(client, headers)
    created = await _create_recording(client, headers, project_id, 1024)
    recording_id = UUID(created.json()["recording"]["id"])
    async with SqlAlchemyUnitOfWork(sessions) as uow:
        recording = await uow.recordings.get(recording_id, owner)
        assert recording is not None
        recording.mark_uploaded()
        recording.start_processing(5.0)
        await uow.stages.reset_incomplete(recording.id)
        stage = await uow.stages.get(recording.id, STAGE)
        assert stage is not None
        stage.start()
        await uow.stages.save(stage)
        await uow.recordings.save(recording)
        uow.add_events(recording.pull_events())
        await uow.commit()
    return owner, recording_id


async def test_a_stale_progress_report_cannot_touch_a_newer_run(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    owner, recording_id = await _processing_recording(api_client, session_factory)
    entered, release = asyncio.Event(), asyncio.Event()

    def pausing() -> UnitOfWork:
        return PausingUnitOfWork(session_factory, entered, release)

    def plain() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    runner = StageRunner(pausing, handlers=[])
    # The fixture starts this stage once, so the report belongs to claim 1.
    stale = asyncio.create_task(runner._report_progress(recording_id, owner, 1, STAGE, 80, claim=1))
    await asyncio.wait_for(entered.wait(), timeout=10)  # it has read the recording of run 1

    async def cancel_and_retry() -> int:
        await CancelRecording(plain).execute(owner, recording_id)
        return (await RetryRecording(plain).execute(owner, recording_id)).run

    newer = asyncio.create_task(cancel_and_retry())
    await asyncio.sleep(1)
    assert not newer.done()  # cancel waits for the row lock the stale report holds

    release.set()
    await stale
    assert await asyncio.wait_for(newer, timeout=10) == 2

    async with plain() as uow:
        stage = await uow.stages.get(recording_id, STAGE)
    assert stage is not None
    assert (stage.status, stage.progress) == (StageStatus.PENDING, 0)  # run 2 starts clean


async def test_concurrent_cancels_succeed_exactly_once(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    owner, recording_id = await _processing_recording(api_client, session_factory)

    def plain() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    results = await asyncio.gather(
        CancelRecording(plain).execute(owner, recording_id),
        CancelRecording(plain).execute(owner, recording_id),
        return_exceptions=True,
    )
    assert sum(isinstance(r, Recording) for r in results) == 1
    assert sum(isinstance(r, InvalidStateError) for r in results) == 1


async def test_cancel_and_retry_racing_leave_a_consistent_state(
    api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    owner, recording_id = await _processing_recording(api_client, session_factory)

    def plain() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    results = await asyncio.gather(
        CancelRecording(plain).execute(owner, recording_id),
        RetryRecording(plain).execute(owner, recording_id),
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) or isinstance(r, InvalidStateError) for r in results)
    async with plain() as uow:
        recording = await uow.recordings.get(recording_id, owner)
    assert recording is not None
    # retry first is a no-op (already processing), then cancel; or cancel first, then retry
    assert (recording.status, recording.run) in {
        (RecordingStatus.CANCELLED, 1),
        (RecordingStatus.PROCESSING, 2),
    }
