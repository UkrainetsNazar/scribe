from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.application.interfaces import ExportRepository
from scribe.domain.exports import Export, ExportFormat, ExportStatus
from scribe.infrastructure.db.models import ExportModel


class SqlAlchemyExportRepository(ExportRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, export: Export) -> None:
        stmt = pg_insert(ExportModel).values(
            id=export.id,
            recording_id=export.recording_id,
            format=export.format.value,
            storage_key=export.storage_key,
            status=export.status.value,
            created_at=export.created_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["recording_id", "format"],
            set_={
                "storage_key": stmt.excluded.storage_key,
                "status": stmt.excluded.status,
                "created_at": stmt.excluded.created_at,
            },
        )
        await self._session.execute(stmt)

    async def get(self, recording_id: UUID, fmt: ExportFormat) -> Export | None:
        stmt = (
            select(ExportModel)
            .where(ExportModel.recording_id == recording_id, ExportModel.format == fmt.value)
            .execution_options(populate_existing=True)  # the row may have just been upserted
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        if model is None:
            return None
        return Export(
            id=model.id,
            recording_id=model.recording_id,
            format=ExportFormat(model.format),
            storage_key=model.storage_key,
            status=ExportStatus(model.status),
            created_at=model.created_at,
        )
