from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeGuard
from uuid import UUID

from scribe.application.interfaces import UnitOfWorkFactory
from scribe.domain.pipeline import PIPELINE_STAGES, PermanentStageError, StageName, StageStatus
from scribe.domain.recordings import Recording, RecordingStatus


class StageOutcome(StrEnum):
    COMPLETED = "completed"
    ALREADY_DONE = "already_done"
    SKIPPED = "skipped"  # the recording was cancelled, deleted or a newer run started


class StageAborted(Exception):
    """Raised inside a handler when the run is no longer current: stop silently."""


@dataclass(frozen=True, slots=True)
class StageResult:
    language: str | None = None


@dataclass(frozen=True, slots=True)
class StageContext:
    recording: Recording
    progress: Callable[[int], Awaitable[None]]  # raises StageAborted when the run is stale


class StageHandler(ABC):
    name: StageName

    @abstractmethod
    async def execute(self, ctx: StageContext) -> StageResult | None: ...


def _is_current(recording: Recording | None, run: int) -> TypeGuard[Recording]:
    return (
        recording is not None
        and recording.status is RecordingStatus.PROCESSING
        and recording.run == run
    )


class StageRunner:
    """Runs one stage and keeps stage rows and the recording state consistent.

    Handlers run outside any transaction (they can take minutes); state changes happen in
    short transactions before and after. Every stage is idempotent, so a repeated execution
    (redelivery after a worker crash) is safe.
    """

    def __init__(self, uow_factory: UnitOfWorkFactory, handlers: Sequence[StageHandler]) -> None:
        self._uow_factory = uow_factory
        self._handlers = {handler.name: handler for handler in handlers}

    async def execute(
        self, recording_id: UUID, owner_id: UUID, run: int, stage_name: StageName
    ) -> StageOutcome:
        handler = self._handlers.get(stage_name)
        if handler is None:
            raise PermanentStageError(f"No handler registered for stage '{stage_name}'")

        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id)
            if not _is_current(recording, run):
                return StageOutcome.SKIPPED
            stage = await uow.stages.get(recording_id, stage_name)
            if stage is None:
                raise PermanentStageError("Processing stages are not initialized")
            if stage.status is StageStatus.COMPLETED:
                return StageOutcome.ALREADY_DONE
            stage.start()
            await uow.stages.save(stage)
            await uow.commit()

        async def report(percent: int) -> None:
            await self._report_progress(recording_id, owner_id, run, stage_name, percent)

        try:
            result = await handler.execute(StageContext(recording=recording, progress=report))
        except StageAborted:
            return StageOutcome.SKIPPED
        return await self._finish(recording_id, owner_id, run, stage_name, result)

    async def mark_failed(
        self, recording_id: UUID, owner_id: UUID, run: int, stage_name: StageName, error: str
    ) -> bool:
        """Fails the stage and the recording. False if the run is no longer current."""
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
            if not _is_current(recording, run):
                return False
            stage = await uow.stages.get(recording_id, stage_name)
            if stage is not None:
                stage.fail(error)
                await uow.stages.save(stage)
            recording.fail(f"{stage_name.value}: {error}"[:500])
            await uow.recordings.save(recording)
            uow.add_events(recording.pull_events())
            await uow.commit()
        return True

    async def _report_progress(
        self, recording_id: UUID, owner_id: UUID, run: int, stage_name: StageName, percent: int
    ) -> None:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id)
            if not _is_current(recording, run):
                raise StageAborted
            stage = await uow.stages.get(recording_id, stage_name)
            if stage is None:
                raise StageAborted
            stage.set_progress(percent)
            await uow.stages.save(stage)
            await uow.commit()

    async def _finish(
        self,
        recording_id: UUID,
        owner_id: UUID,
        run: int,
        stage_name: StageName,
        result: StageResult | None,
    ) -> StageOutcome:
        async with self._uow_factory() as uow:
            # Row lock: serializes with cancel/retry; a late result of a stale run is dropped
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
            if not _is_current(recording, run):
                return StageOutcome.SKIPPED
            stage = await uow.stages.get(recording_id, stage_name)
            if stage is None:
                raise PermanentStageError("Processing stages are not initialized")
            stage.complete()
            await uow.stages.save(stage)
            if result is not None and result.language:
                recording.set_language(result.language)

            stages = await uow.stages.list_for_recording(recording_id)
            if len(stages) == len(PIPELINE_STAGES) and all(
                s.status is StageStatus.COMPLETED for s in stages
            ):
                recording.complete()
            await uow.recordings.save(recording)
            uow.add_events(recording.pull_events())
            await uow.commit()
        return StageOutcome.COMPLETED
