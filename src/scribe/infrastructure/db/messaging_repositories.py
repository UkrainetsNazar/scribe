from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.application.messaging import InboxRepository, OutboxMessage
from scribe.application.outbox import OutboxRecord
from scribe.infrastructure.db.models import InboxMessageModel, OutboxEventModel


class SqlAlchemyOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_many(self, records: Sequence[OutboxRecord]) -> None:
        if not records:
            return
        self._session.add_all(
            [
                OutboxEventModel(
                    event_id=r.event_id,
                    aggregate_type=r.aggregate_type,
                    aggregate_id=r.aggregate_id,
                    event_type=r.event_type,
                    payload=r.payload,
                    headers=r.headers,
                )
                for r in records
            ]
        )
        await self._session.flush()

    async def fetch_unpublished(self, limit: int) -> list[OutboxMessage]:
        """Locks a batch. Rows locked by another relay are skipped, so relays never overlap."""
        stmt = (
            select(OutboxEventModel)
            .where(OutboxEventModel.published_at.is_(None))
            .order_by(OutboxEventModel.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            OutboxMessage(
                id=m.id,
                event_id=m.event_id,
                event_type=m.event_type,
                payload=m.payload,
                headers=m.headers,
                created_at=m.created_at,
            )
            for m in rows
        ]

    async def mark_published(self, ids: Sequence[int]) -> None:
        await self._session.execute(
            update(OutboxEventModel)
            .where(OutboxEventModel.id.in_(ids))
            .values(published_at=func.now())
            .execution_options(synchronize_session=False)
        )


class SqlAlchemyInboxRepository(InboxRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(self, message_id: UUID, consumer: str) -> bool:
        stmt = (
            pg_insert(InboxMessageModel)
            .values(message_id=message_id, consumer=consumer)
            .on_conflict_do_nothing(index_elements=["message_id", "consumer"])
            .returning(InboxMessageModel.message_id)
        )
        # If a concurrent transaction inserted the same key, this waits until it finishes
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def mark_processed(self, message_id: UUID, consumer: str) -> None:
        await self._session.execute(
            update(InboxMessageModel)
            .where(
                InboxMessageModel.message_id == message_id,
                InboxMessageModel.consumer == consumer,
            )
            .values(processed_at=func.now())
        )
