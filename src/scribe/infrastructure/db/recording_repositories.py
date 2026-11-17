from collections.abc import Collection
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.application.interfaces import RecordingRepository
from scribe.application.pagination import Cursor
from scribe.domain.recordings import Recording, RecordingStatus
from scribe.infrastructure.db.models import RecordingModel


def _to_domain(m: RecordingModel) -> Recording:
    return Recording(
        id=m.id,
        project_id=m.project_id,
        owner_id=m.owner_id,
        title=m.title,
        source_filename=m.source_filename,
        content_type=m.content_type,
        size_bytes=m.size_bytes,
        language=m.language,
        storage_key=m.storage_key,
        status=RecordingStatus(m.status),
        duration_sec=m.duration_sec,
        run=m.run,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


class SqlAlchemyRecordingRepository(RecordingRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, recording: Recording) -> None:
        self._session.add(
            RecordingModel(
                id=recording.id,
                project_id=recording.project_id,
                owner_id=recording.owner_id,
                title=recording.title,
                source_filename=recording.source_filename,
                content_type=recording.content_type,
                size_bytes=recording.size_bytes,
                duration_sec=recording.duration_sec,
                language=recording.language,
                status=recording.status.value,
                storage_key=recording.storage_key,
                run=recording.run,
                created_at=recording.created_at,
                updated_at=recording.updated_at,
            )
        )
        await self._session.flush()

    async def get(
        self, recording_id: UUID, owner_id: UUID, *, for_update: bool = False
    ) -> Recording | None:
        stmt = select(RecordingModel).where(
            RecordingModel.id == recording_id,
            RecordingModel.owner_id == owner_id,
            RecordingModel.status != RecordingStatus.DELETED.value,
        )
        if for_update:
            stmt = stmt.with_for_update()
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model else None

    async def list_by_owner(
        self,
        owner_id: UUID,
        *,
        project_id: UUID | None,
        status: RecordingStatus | None,
        limit: int,
        cursor: Cursor | None,
    ) -> list[Recording]:
        stmt = select(RecordingModel).where(
            RecordingModel.owner_id == owner_id,
            RecordingModel.status != RecordingStatus.DELETED.value,
        )
        if project_id is not None:
            stmt = stmt.where(RecordingModel.project_id == project_id)
        if status is not None:
            stmt = stmt.where(RecordingModel.status == status.value)
        if cursor is not None:
            stmt = stmt.where(
                or_(
                    RecordingModel.created_at < cursor.created_at,
                    and_(
                        RecordingModel.created_at == cursor.created_at,
                        RecordingModel.id < cursor.id,
                    ),
                )
            )
        stmt = stmt.order_by(RecordingModel.created_at.desc(), RecordingModel.id.desc()).limit(
            limit + 1
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(m) for m in rows]

    async def filter_visible_ids(
        self, owner_id: UUID, recording_ids: Collection[UUID], project_id: UUID | None
    ) -> set[UUID]:
        if not recording_ids:
            return set()
        stmt = select(RecordingModel.id).where(
            RecordingModel.id.in_(list(recording_ids)),
            RecordingModel.owner_id == owner_id,
            RecordingModel.status == RecordingStatus.COMPLETED.value,
        )
        if project_id is not None:
            stmt = stmt.where(RecordingModel.project_id == project_id)
        return set((await self._session.execute(stmt)).scalars().all())

    async def purge(self, recording_id: UUID) -> bool:
        result = await self._session.execute(
            delete(RecordingModel).where(
                RecordingModel.id == recording_id,
                RecordingModel.status == RecordingStatus.DELETED.value,
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def save(self, recording: Recording) -> None:
        await self._session.execute(
            update(RecordingModel)
            .where(RecordingModel.id == recording.id)
            .values(
                status=recording.status.value,
                duration_sec=recording.duration_sec,
                run=recording.run,
                language=recording.language,
                updated_at=recording.updated_at,
            )
        )
