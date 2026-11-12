from uuid import UUID

import pytest

from scribe.application.event_handlers import PipelineStartHandler
from scribe.application.inbox import InboundEvent, PermanentHandlerError
from scribe.application.interfaces import UnitOfWorkFactory
from scribe.application.pipeline import (
    StageContext,
    StageHandler,
    StageOutcome,
    StageResult,
    StageRunner,
)
from scribe.domain.events import RecordingCompleted, RecordingFailed
from scribe.domain.pipeline import PIPELINE_STAGES, StageName, StageStatus
from scribe.domain.recordings import Recording, RecordingStatus
from tests.unit.fakes import FakeDb, FakeDispatcher, FakeProgressPublisher, FakeUnitOfWork
from tests.unit.test_domain_recording import make_recording


class FakeHandler(StageHandler):
    def __init__(
        self,
        name: StageName,
        *,
        result: StageResult | None = None,
        error: Exception | None = None,
        cancel_first: FakeDb | None = None,
    ) -> None:
        self.name = name
        self.calls = 0
        self._result = result
        self._error = error
        self._cancel_first = cancel_first

    async def execute(self, ctx: StageContext) -> StageResult | None:
        self.calls += 1
        if self._error is not None:
            raise self._error
        if self._cancel_first is not None:  # simulate `cancel` arriving mid-stage
            self._cancel_first.recordings[ctx.recording.id].status = RecordingStatus.CANCELLED
            await ctx.progress(50)
        return self._result


def _runner(
    uow_factory: UnitOfWorkFactory, overrides: dict[StageName, FakeHandler] | None = None
) -> tuple[StageRunner, dict[StageName, FakeHandler]]:
    handlers = {name: FakeHandler(name) for name in PIPELINE_STAGES}
    handlers.update(overrides or {})
    return StageRunner(uow_factory, list(handlers.values())), handlers


async def _processing(db: FakeDb, uow_factory: UnitOfWorkFactory) -> Recording:
    recording = make_recording()
    recording.mark_uploaded()
    recording.start_processing(60.0)
    recording.pull_events()
    db.recordings[recording.id] = recording
    async with uow_factory() as uow:
        await uow.stages.reset_incomplete(recording.id)
    return recording


async def test_all_stages_complete_the_recording(
    db: FakeDb, uow_factory: UnitOfWorkFactory
) -> None:
    runner, _ = _runner(
        uow_factory,
        {
            StageName.TRANSCRIBE: FakeHandler(
                StageName.TRANSCRIBE, result=StageResult(language="uk")
            )
        },
    )
    recording = await _processing(db, uow_factory)

    for name in PIPELINE_STAGES:
        outcome = await runner.execute(recording.id, recording.owner_id, 1, name)
        assert outcome is StageOutcome.COMPLETED
        expected = (
            RecordingStatus.COMPLETED if name is StageName.INDEX else RecordingStatus.PROCESSING
        )
        assert recording.status is expected

    assert recording.language == "uk"
    assert all(s.status is StageStatus.COMPLETED for s in db.stages.values())
    assert [type(e) for e in db.events] == [RecordingCompleted]


async def test_stale_run_and_cancelled_recording_are_skipped(
    db: FakeDb, uow_factory: UnitOfWorkFactory
) -> None:
    runner, handlers = _runner(uow_factory)
    recording = await _processing(db, uow_factory)
    first = StageName.EXTRACT_AUDIO

    assert await runner.execute(recording.id, recording.owner_id, 99, first) is StageOutcome.SKIPPED
    recording.cancel()
    assert await runner.execute(recording.id, recording.owner_id, 1, first) is StageOutcome.SKIPPED
    assert handlers[first].calls == 0
    assert db.stages[(recording.id, first)].status is StageStatus.PENDING


async def test_completed_stage_is_not_repeated(db: FakeDb, uow_factory: UnitOfWorkFactory) -> None:
    runner, handlers = _runner(uow_factory)
    recording = await _processing(db, uow_factory)
    first = StageName.EXTRACT_AUDIO

    await runner.execute(recording.id, recording.owner_id, 1, first)
    again = await runner.execute(recording.id, recording.owner_id, 1, first)
    assert again is StageOutcome.ALREADY_DONE
    assert handlers[first].calls == 1


async def test_cancel_during_stage_is_noticed_at_progress_report(
    db: FakeDb, uow_factory: UnitOfWorkFactory
) -> None:
    first = StageName.EXTRACT_AUDIO
    runner, _ = _runner(uow_factory, {first: FakeHandler(first, cancel_first=db)})
    recording = await _processing(db, uow_factory)

    outcome = await runner.execute(recording.id, recording.owner_id, 1, first)
    assert outcome is StageOutcome.SKIPPED
    assert db.stages[(recording.id, first)].status is StageStatus.RUNNING  # reset by retry later


async def test_handler_error_propagates_and_mark_failed_fails_the_recording(
    db: FakeDb, uow_factory: UnitOfWorkFactory
) -> None:
    first = StageName.EXTRACT_AUDIO
    runner, _ = _runner(uow_factory, {first: FakeHandler(first, error=RuntimeError("boom"))})
    recording = await _processing(db, uow_factory)

    with pytest.raises(RuntimeError):
        await runner.execute(recording.id, recording.owner_id, 1, first)
    stage = db.stages[(recording.id, first)]
    assert (stage.status, stage.attempts) == (StageStatus.RUNNING, 1)

    assert await runner.mark_failed(recording.id, recording.owner_id, 1, first, "boom") is True
    assert recording.status is RecordingStatus.FAILED
    assert stage.status is StageStatus.FAILED
    assert [type(e) for e in db.events] == [RecordingFailed]
    # A second call for the same (now failed) run does nothing
    assert await runner.mark_failed(recording.id, recording.owner_id, 1, first, "x") is False


async def test_pipeline_start_handler() -> None:
    dispatcher = FakeDispatcher()
    handler = PipelineStartHandler(dispatcher)
    uow = FakeUnitOfWork(FakeDb())
    recording_id, owner_id = UUID(int=1), UUID(int=2)
    event = InboundEvent(
        event_id=UUID(int=3),
        event_type="recording.processing_requested",
        payload={"recording_id": str(recording_id), "owner_id": str(owner_id), "run": 2},
        headers={},
    )

    await handler.handle(event, uow)
    assert dispatcher.dispatched == [(recording_id, owner_id, 2)]

    broken = InboundEvent(event_id=UUID(int=4), event_type="x", payload={}, headers={})
    with pytest.raises(PermanentHandlerError):
        await handler.handle(broken, uow)


async def test_runner_publishes_progress_for_every_stage(
    db: FakeDb, uow_factory: UnitOfWorkFactory
) -> None:
    events = FakeProgressPublisher()
    runner = StageRunner(uow_factory, [FakeHandler(name) for name in PIPELINE_STAGES], events)
    recording = await _processing(db, uow_factory)

    for name in PIPELINE_STAGES:
        await runner.execute(recording.id, recording.owner_id, 1, name)

    messages = [message for _, message in events.states]
    assert [(m["stage"], m["percent"]) for m in messages] == [
        (name.value, percent) for name in PIPELINE_STAGES for percent in (0, 100)
    ]
    assert messages[-1]["overall"] == 100
    assert all(m["run"] == 1 and m["type"] == "progress" for m in messages)
