from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.application.interfaces import ProjectRepository
from scribe.application.pagination import Cursor
from scribe.domain.errors import ProjectNameTakenError
from scribe.domain.projects import Project
from scribe.infrastructure.db.models import ProjectModel


def _to_domain(m: ProjectModel) -> Project:
    return Project(
        id=m.id,
        owner_id=m.owner_id,
        name=m.name,
        description=m.description,
        created_at=m.created_at,
        deleted_at=m.deleted_at,
    )


class SqlAlchemyProjectRepository(ProjectRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, project: Project) -> None:
        self._session.add(
            ProjectModel(
                id=project.id,
                owner_id=project.owner_id,
                name=project.name,
                description=project.description,
                created_at=project.created_at,
                deleted_at=project.deleted_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ProjectNameTakenError from exc

    async def get(self, project_id: UUID, owner_id: UUID) -> Project | None:
        stmt = select(ProjectModel).where(
            ProjectModel.id == project_id,
            ProjectModel.owner_id == owner_id,
            ProjectModel.deleted_at.is_(None),
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model else None

    async def get_by_name(self, owner_id: UUID, name: str) -> Project | None:
        stmt = select(ProjectModel).where(
            ProjectModel.owner_id == owner_id,
            ProjectModel.name == name,
            ProjectModel.deleted_at.is_(None),
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_domain(model) if model else None

    async def list_by_owner(
        self, owner_id: UUID, *, limit: int, cursor: Cursor | None
    ) -> list[Project]:
        stmt = select(ProjectModel).where(
            ProjectModel.owner_id == owner_id, ProjectModel.deleted_at.is_(None)
        )
        if cursor is not None:
            stmt = stmt.where(
                or_(
                    ProjectModel.created_at < cursor.created_at,
                    and_(
                        ProjectModel.created_at == cursor.created_at,
                        ProjectModel.id < cursor.id,
                    ),
                )
            )
        stmt = stmt.order_by(ProjectModel.created_at.desc(), ProjectModel.id.desc()).limit(
            limit + 1
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(m) for m in rows]

    async def save(self, project: Project) -> None:
        stmt = (
            update(ProjectModel)
            .where(ProjectModel.id == project.id)
            .values(
                name=project.name,
                description=project.description,
                deleted_at=project.deleted_at,
            )
        )
        try:
            await self._session.execute(stmt)
        except IntegrityError as exc:
            raise ProjectNameTakenError from exc
