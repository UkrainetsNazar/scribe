import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import aio_pika
import orjson
import pytest
from aio_pika.abc import AbstractIncomingMessage
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.application.inbox import EventHandler, InboxGuard
from scribe.application.interfaces import UnitOfWork
from scribe.application.messaging import OutboxMessage
from scribe.domain.events import RecordingProcessingRequested
from scribe.infrastructure.db.models import OutboxEventModel
from scribe.infrastructure.db.uow import SqlAlchemyUnitOfWork
from scribe.infrastructure.messaging.consumer import RabbitMqConsumer
from scribe.infrastructure.messaging.publisher import RabbitMqPublisher
from scribe.infrastructure.messaging.relay import OutboxRelay
from scribe.infrastructure.messaging.topology import PIPELINE_START
from tests.integration.conftest import RabbitBroker
from tests.unit.fakes import CountingHandler

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
EVENT_TYPE = "recording.processing_requested"
QUEUE = "pipeline.start"
DLQ = "pipeline.start.dlq"


@pytest.fixture(autouse=True)
async def _clean_state(rabbit: RabbitBroker, session_factory: Sessions) -> None:
    async with session_factory() as session:
        await session.execute(delete(OutboxEventModel))
        await session.commit()
    connection = await aio_pika.connect(rabbit.url)
    async with connection:
        channel = await connection.channel()
        for name in (QUEUE, DLQ):
            queue = await channel.declare_queue(name, durable=True)
            await queue.purge()


def _event() -> RecordingProcessingRequested:
    return RecordingProcessingRequested(
        recording_id=uuid4(), owner_id=uuid4(), storage_key="u/r/source/a.wav"
    )


def _message(event_id: UUID | None = None) -> OutboxMessage:
    return OutboxMessage(
        id=1,
        event_id=event_id or uuid4(),
        event_type=EVENT_TYPE,
        payload={"recording_id": str(uuid4()), "owner_id": str(uuid4()), "storage_key": "k"},
        headers={},
        created_at=datetime.now(UTC),
    )


async def _store_event(session_factory: Sessions, event: RecordingProcessingRequested) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.add_events([event])
        await uow.commit()


async def _unpublished(session_factory: Sessions) -> int:
    async with session_factory() as session:
        stmt = (
            select(func.count())
            .select_from(OutboxEventModel)
            .where(OutboxEventModel.published_at.is_(None))
        )
        return int((await session.execute(stmt)).scalar_one())


async def _get_one(url: str, queue_name: str, timeout: float = 10.0) -> AbstractIncomingMessage:  # noqa: ASYNC109
    connection = await aio_pika.connect(url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(queue_name, durable=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = await queue.get(fail=False, no_ack=True)
            if message is not None:
                return message
            await asyncio.sleep(0.1)
    raise AssertionError(f"no message arrived in '{queue_name}' within {timeout}s")


async def _wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> None:  # noqa: ASYNC109
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.1)


async def _start_consumer(
    rabbit: RabbitBroker,
    session_factory: Sessions,
    handler: EventHandler,
    delays: tuple[int, ...],
) -> RabbitMqConsumer:
    def uow_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    guard = InboxGuard(uow_factory, consumer=QUEUE, handler=handler)
    consumer = RabbitMqConsumer(rabbit.url, PIPELINE_START, guard, delays, prefetch=5)
    await consumer.start()
    return consumer


async def test_relay_publishes_outbox_events(
    rabbit: RabbitBroker, session_factory: Sessions
) -> None:
    publisher = RabbitMqPublisher(rabbit.url, (1,), timeout_sec=5)
    await publisher.connect()
    try:
        relay = OutboxRelay(session_factory, publisher, batch_size=10, poll_interval_sec=0.05)
        event = _event()
        await _store_event(session_factory, event)

        assert await relay.run_once() == 1

        message = await _get_one(rabbit.url, QUEUE)
        assert message.message_id == str(event.event_id)
        assert message.type == EVENT_TYPE
        assert orjson.loads(message.body)["recording_id"] == str(event.recording_id)
        assert await relay.run_once() == 0  # already marked as published
    finally:
        await publisher.close()


async def test_events_survive_a_broker_outage(
    rabbit: RabbitBroker, session_factory: Sessions
) -> None:
    publisher = RabbitMqPublisher(rabbit.url, (1,), timeout_sec=3)
    await publisher.connect()
    try:
        relay = OutboxRelay(session_factory, publisher, batch_size=10, poll_interval_sec=0.05)
        event = _event()
        await _store_event(session_factory, event)

        await asyncio.to_thread(rabbit.stop)
        try:
            failed = False
            try:
                await relay.run_once()
            except Exception:
                failed = True
            assert failed
            assert await _unpublished(session_factory) == 1  # not lost, not marked as published
        finally:
            await asyncio.to_thread(rabbit.start)
            await asyncio.to_thread(rabbit.wait_ready)

        published = 0
        for _ in range(60):  # the robust connection needs a few seconds to reconnect
            try:
                published = await relay.run_once()
            except Exception:
                published = 0
            if published:
                break
            await asyncio.sleep(1)
        assert published == 1

        message = await _get_one(rabbit.url, QUEUE)
        assert message.message_id == str(event.event_id)
        assert await _unpublished(session_factory) == 0
    finally:
        await publisher.close()


async def test_duplicate_delivery_is_processed_once(
    rabbit: RabbitBroker, session_factory: Sessions
) -> None:
    publisher = RabbitMqPublisher(rabbit.url, (1,), timeout_sec=5)
    await publisher.connect()
    handler = CountingHandler()
    consumer = await _start_consumer(rabbit, session_factory, handler, (1,))
    try:
        message = _message()
        await publisher.publish(message)
        await publisher.publish(message)  # the same message_id delivered twice

        await _wait_for(lambda: handler.calls >= 1)
        await asyncio.sleep(1)  # give the duplicate time to arrive
        assert handler.calls == 1
    finally:
        await consumer.close()
        await publisher.close()


async def test_failed_message_is_retried_then_processed(
    rabbit: RabbitBroker, session_factory: Sessions
) -> None:
    publisher = RabbitMqPublisher(rabbit.url, (1, 1), timeout_sec=5)
    await publisher.connect()
    handler = CountingHandler(fail_times=2)
    consumer = await _start_consumer(rabbit, session_factory, handler, (1, 1))
    try:
        await publisher.publish(_message())
        await _wait_for(lambda: handler.calls == 3, timeout=20)  # 2 failures + 1 success
    finally:
        await consumer.close()
        await publisher.close()


async def test_exhausted_retries_go_to_dlq(rabbit: RabbitBroker, session_factory: Sessions) -> None:
    publisher = RabbitMqPublisher(rabbit.url, (1,), timeout_sec=5)
    await publisher.connect()
    handler = CountingHandler(fail_times=100)
    consumer = await _start_consumer(rabbit, session_factory, handler, (1,))
    try:
        message = _message()
        await publisher.publish(message)

        dead = await _get_one(rabbit.url, DLQ, timeout=20)
        assert dead.message_id == str(message.event_id)
        assert dead.headers["x-retry-count"] == 1
        assert "retries exhausted" in str(dead.headers["x-dead-letter-reason"])
        assert handler.calls == 2  # the first attempt + one retry
    finally:
        await consumer.close()
        await publisher.close()
