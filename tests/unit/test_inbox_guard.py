from uuid import uuid4

import pytest

from scribe.application.inbox import InboundEvent, InboxGuard
from scribe.application.interfaces import UnitOfWorkFactory
from tests.unit.fakes import CountingHandler


def make_event() -> InboundEvent:
    return InboundEvent(event_id=uuid4(), event_type="recording.created", payload={}, headers={})


async def test_duplicate_is_processed_once(uow_factory: UnitOfWorkFactory) -> None:
    handler = CountingHandler()
    guard = InboxGuard(uow_factory, "consumer-a", handler)
    event = make_event()

    assert await guard.process(event) is True
    assert await guard.process(event) is False
    assert handler.calls == 1


async def test_failed_handler_leaves_no_inbox_record(uow_factory: UnitOfWorkFactory) -> None:
    handler = CountingHandler(fail_times=1)
    guard = InboxGuard(uow_factory, "consumer-a", handler)
    event = make_event()

    with pytest.raises(RuntimeError):
        await guard.process(event)
    assert await guard.process(event) is True  # the retry is not mistaken for a duplicate
    assert handler.calls == 2


async def test_consumers_have_independent_inboxes(uow_factory: UnitOfWorkFactory) -> None:
    first, second = CountingHandler(), CountingHandler()
    event = make_event()

    assert await InboxGuard(uow_factory, "consumer-a", first).process(event) is True
    assert await InboxGuard(uow_factory, "consumer-b", second).process(event) is True
    assert first.calls == second.calls == 1
