import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.application.inbox import InboundEvent, InboxGuard
from scribe.domain.events import RecordingProcessingRequested
from scribe.domain.users import User
from scribe.infrastructure.db.models import OutboxEventModel
from scribe.infrastructure.db.uow import SqlAlchemyUnitOfWork
from tests.unit.fakes import CountingHandler

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


def _event() -> RecordingProcessingRequested:
    return RecordingProcessingRequested(
        recording_id=uuid4(), owner_id=uuid4(), storage_key="u/r/source/a.wav"
    )


async def _rows(session_factory: Sessions, event_id: UUID) -> list[OutboxEventModel]:
    async with session_factory() as session:
        result = await session.execute(
            select(OutboxEventModel).where(OutboxEventModel.event_id == event_id)
        )
        return list(result.scalars().all())


async def test_commit_writes_event_to_outbox(session_factory: Sessions) -> None:
    event = _event()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.add_events([event])
        await uow.commit()

    (row,) = await _rows(session_factory, event.event_id)
    assert row.event_type == "recording.processing_requested"
    assert row.aggregate_type == "recording"
    assert row.aggregate_id == event.recording_id
    assert row.payload["storage_key"] == "u/r/source/a.wav"
    assert row.published_at is None


async def test_rollback_discards_events(session_factory: Sessions) -> None:
    event = _event()
    with pytest.raises(RuntimeError):
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            uow.add_events([event])
            raise RuntimeError("crash before commit")
    assert await _rows(session_factory, event.event_id) == []


async def test_aggregate_and_event_commit_together(session_factory: Sessions) -> None:
    event = _event()
    email = f"{uuid4().hex[:10]}@example.com"
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        plan = await uow.plans.get_by_name("free")
        assert plan is not None
        await uow.users.add(User.register(email, "hash", plan))
        uow.add_events([event])
        await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.users.get_by_email(email) is not None
    assert len(await _rows(session_factory, event.event_id)) == 1


async def test_failed_outbox_insert_rolls_back_the_aggregate(session_factory: Sessions) -> None:
    event = _event()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.add_events([event])
        await uow.commit()

    email = f"{uuid4().hex[:10]}@example.com"
    with pytest.raises(IntegrityError):  # the same event_id violates the unique constraint
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            plan = await uow.plans.get_by_name("free")
            assert plan is not None
            await uow.users.add(User.register(email, "hash", plan))
            uow.add_events([event])
            await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.users.get_by_email(email) is None  # nothing was half-committed


async def test_inbox_serializes_concurrent_duplicates(session_factory: Sessions) -> None:
    handler = CountingHandler(delay=0.3)
    guard = InboxGuard(lambda: SqlAlchemyUnitOfWork(session_factory), "concurrency-test", handler)
    event = InboundEvent(event_id=uuid4(), event_type="x", payload={}, headers={})

    results = await asyncio.gather(guard.process(event), guard.process(event))

    assert sorted(results) == [False, True]
    assert handler.calls == 1
