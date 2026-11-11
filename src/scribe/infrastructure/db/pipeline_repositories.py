from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.application.interfaces import StageRepository, UsageRepository
from scribe.domain.pipeline import PIPELINE_STAGES, ProcessingStage, StageName, StageStatus
from scribe.infrastructure.db.models import ProcessingStageModel, UsageModel


def _to_domain(m: ProcessingStageModel) -> ProcessingStage:
    return ProcessingStage(
        recording_id=m.recording_id,
        name=StageName(m.name),
        status=StageStatus(m.status),
        progress=m.progress,
        attempts=m.attempts,
        started_at=m.started_at,
        finished_at=m.finished_at,
        error=m.error,
    )


class SqlAlchemyStageRepository(StageRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reset_incomplete(self, recording_id: UUID) -> None:
        rows = [
            {
                "recording_id": recording_id,
                "name": stage.value,
                "status": StageStatus.PENDING.value,
                "progress": 0,
                "attempts": 0,
            }
            for stage in PIPELINE_STAGES
        ]
        stmt = pg_insert(ProcessingStageModel).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["recording_id", "name"],
            set_={
                "status": StageStatus.PENDING.value,
                "progress": 0,
                "error": None,
                "started_at": None,
                "finished_at": None,
            },
            where=ProcessingStageModel.status != StageStatus.COMPLETED.value,
        )
        await self._session.execute(stmt)

    async def get(self, recording_id: UUID, name: StageName) -> ProcessingStage | None:
        stmt = (
            select(ProcessingStageModel)
            .where(
                ProcessingStageModel.recording_id == recording_id,
                ProcessingStageModel.name == name.value,
            )
            # The row may have been changed by a Core UPDATE in this session
            .execution_options(populate_existing=True)
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model else None

    async def list_for_recording(self, recording_id: UUID) -> list[ProcessingStage]:
        stmt = (
            select(ProcessingStageModel)
            .where(ProcessingStageModel.recording_id == recording_id)
            .execution_options(populate_existing=True)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        stages = [_to_domain(m) for m in rows]
        return sorted(stages, key=lambda s: PIPELINE_STAGES.index(s.name))

    async def save(self, stage: ProcessingStage) -> None:
        await self._session.execute(
            update(ProcessingStageModel)
            .where(
                ProcessingStageModel.recording_id == stage.recording_id,
                ProcessingStageModel.name == stage.name.value,
            )
            .values(
                status=stage.status.value,
                progress=stage.progress,
                attempts=stage.attempts,
                started_at=stage.started_at,
                finished_at=stage.finished_at,
                error=stage.error,
            )
        )


class SqlAlchemyUsageRepository(UsageRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_minutes(self, user_id: UUID, period: str) -> int:
        await self._session.execute(
            pg_insert(UsageModel)
            .values(user_id=user_id, period=period, minutes_used=0)
            .on_conflict_do_nothing(index_elements=["user_id", "period"])
        )
        result = await self._session.execute(
            select(UsageModel.minutes_used)
            .where(UsageModel.user_id == user_id, UsageModel.period == period)
            .with_for_update()
        )
        return int(result.scalar_one())

    async def add_minutes(self, user_id: UUID, period: str, minutes: int) -> None:
        await self._session.execute(
            update(UsageModel)
            .where(UsageModel.user_id == user_id, UsageModel.period == period)
            .values(minutes_used=UsageModel.minutes_used + minutes)
        )

    async def get_minutes(self, user_id: UUID, period: str) -> int:
        result = await self._session.execute(
            select(UsageModel.minutes_used).where(
                UsageModel.user_id == user_id, UsageModel.period == period
            )
        )
        return int(result.scalar_one_or_none() or 0)
