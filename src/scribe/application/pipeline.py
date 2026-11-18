from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeGuard
from uuid import UUID

from scribe.application.interfaces import UnitOfWorkFactory
from scribe.application.progress import NullProgressPublisher, ProgressPublisher, progress_message
from scribe.domain.pipeline import (
    PIPELINE_STAGES,
    PermanentStageError,
    ProcessingStage,
    StageName,
    StageStatus,
)
from scribe.domain.recordings import Recording, RecordingStatus


class StageOutcome(StrEnum):
    COMPLETED = "completed"
    ALREADY_DONE = "already_done"
    SKIPPED = "skipped"  # cancelled, deleted, a newer run, or a newer execution of the stage


class StageAborted(Exception):
    """Raised inside a handler when this execution is no longer the current one: stop silently."""


class StageExecutionError(Exception):
    """A stage handler failed. `claim` identifies the execution that failed, so marking the stage
    as failed cannot affect a newer execution of the same stage."""

    def __init__(self, claim: int, cause: Exception) -> None:
        super().__init__(repr(cause))
        self.claim = claim
        self.cause = cause


@dataclass(frozen=True, slots=True)
class StageResult:
    language: str | None = None


@dataclass(frozen=True, slots=True)
class StageContext:
    recording: Recording
    progress: Callable[[int], Awaitable[None]]  # raises StageAborted when this execution is stale


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


def _is_claimed(stage: ProcessingStage | None, claim: int) -> TypeGuard[ProcessingStage]:
    """True while nobody has started a newer attempt of the stage."""
    return stage is not None and stage.status is StageStatus.RUNNING and stage.attempts == claim


class StageRunner:
    """Runs one stage and keeps stage rows and the recording state consistent.

    Handlers run outside any transaction (they can take minutes); state changes happen in short
    transactions before and after. Every transaction that changes a stage first takes the row lock
    of the recording, the same lock cancel and retry take: a task of an older run cannot touch a
    newer one. The same stage can be delivered twice, so starting it takes a numbered claim
    (`attempts`); only the holder of the latest claim may report progress, complete or fail the
    stage. Stage outputs are idempotent, so a superseded execution is simply dropped.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        handlers: Sequence[StageHandler],
        events: ProgressPublisher | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._handlers = {handler.name: handler for handler in handlers}
        self._events = events or NullProgressPublisher()

    async def execute(
        self, recording_id: UUID, owner_id: UUID, run: int, stage_name: StageName
    ) -> StageOutcome:
        handler = self._handlers.get(stage_name)
        if handler is None:
            raise PermanentStageError(f"No handler registered for stage '{stage_name}'")

        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
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
            claim = stage.attempts
        await self._emit_progress(recording_id, run, stage_name, 0, claim)

        async def report(percent: int) -> None:
            await self._report_progress(recording_id, owner_id, run, stage_name, percent, claim)

        try:
            result = await handler.execute(StageContext(recording=recording, progress=report))
        except StageAborted:
            return StageOutcome.SKIPPED
        except Exception as exc:
            raise StageExecutionError(claim, exc) from exc
        return await self._finish(recording_id, owner_id, run, stage_name, result, claim)

    async def mark_failed(
        self,
        recording_id: UUID,
        owner_id: UUID,
        run: int,
        stage_name: StageName,
        error: str,
        claim: int | None = None,
    ) -> bool:
        """Fails the stage and the recording. False if the run (or, with `claim`, the execution)
        is no longer current."""
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
            if not _is_current(recording, run):
                return False
            stage = await uow.stages.get(recording_id, stage_name)
            if claim is not None and not _is_claimed(stage, claim):
                return False  # a newer execution owns the stage
            if stage is not None:
                stage.fail(error)
                await uow.stages.save(stage)
            recording.fail(error[:500], stage=stage_name.value)
            await uow.recordings.save(recording)
            uow.add_events(recording.pull_events())
            await uow.commit()
        return True

    async def _emit_progress(
        self, recording_id: UUID, run: int, stage_name: StageName, percent: int, attempt: int
    ) -> None:
        await self._events.publish_state(
            recording_id, progress_message(recording_id, run, stage_name, percent, attempt)
        )

    async def _report_progress(
        self,
        recording_id: UUID,
        owner_id: UUID,
        run: int,
        stage_name: StageName,
        percent: int,
        claim: int,
    ) -> None:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
            if not _is_current(recording, run):
                raise StageAborted
            stage = await uow.stages.get(recording_id, stage_name)
            if not _is_claimed(stage, claim):
                raise StageAborted
            stage.set_progress(percent)
            await uow.stages.save(stage)
            await uow.commit()
        await self._emit_progress(recording_id, run, stage_name, percent, claim)

    async def _finish(
        self,
        recording_id: UUID,
        owner_id: UUID,
        run: int,
        stage_name: StageName,
        result: StageResult | None,
        claim: int,
    ) -> StageOutcome:
        async with self._uow_factory() as uow:
            recording = await uow.recordings.get(recording_id, owner_id, for_update=True)
            if not _is_current(recording, run):
                return StageOutcome.SKIPPED
            stage = await uow.stages.get(recording_id, stage_name)
            if not _is_claimed(stage, claim):
                return StageOutcome.SKIPPED
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
        await self._emit_progress(recording_id, run, stage_name, 100, claim)
        return StageOutcome.COMPLETED
