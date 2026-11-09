from collections.abc import Mapping
from uuid import UUID

from scribe.application.interfaces import UnitOfWorkFactory
from scribe.application.pagination import Cursor, Page, build_page, decode_cursor
from scribe.domain.errors import NotFoundError, ProjectNameTakenError
from scribe.domain.projects import Project


class CreateProject:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, name: str, description: str | None) -> Project:
        project = Project.create(owner_id, name, description)
        async with self._uow_factory() as uow:
            if await uow.projects.get_by_name(owner_id, project.name) is not None:
                raise ProjectNameTakenError
            await uow.projects.add(project)
            await uow.commit()
        return project


class ListProjects:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, limit: int, cursor: str | None) -> Page[Project]:
        position = decode_cursor(cursor) if cursor else None
        async with self._uow_factory() as uow:
            rows = await uow.projects.list_by_owner(owner_id, limit=limit, cursor=position)
        return build_page(rows, limit, lambda p: Cursor(p.created_at, p.id))


class GetProject:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, project_id: UUID) -> Project:
        async with self._uow_factory() as uow:
            project = await uow.projects.get(project_id, owner_id)
        if project is None:
            raise NotFoundError("Project not found")
        return project


class UpdateProject:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self, owner_id: UUID, project_id: UUID, changes: Mapping[str, str | None]
    ) -> Project:
        async with self._uow_factory() as uow:
            project = await uow.projects.get(project_id, owner_id)
            if project is None:
                raise NotFoundError("Project not found")
            project.update(changes)
            if "name" in changes:
                existing = await uow.projects.get_by_name(owner_id, project.name)
                if existing is not None and existing.id != project.id:
                    raise ProjectNameTakenError
            await uow.projects.save(project)
            await uow.commit()
        return project


class DeleteProject:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, project_id: UUID) -> None:
        async with self._uow_factory() as uow:
            project = await uow.projects.get(project_id, owner_id)
            if project is None:
                raise NotFoundError("Project not found")
            project.delete()
            await uow.projects.save(project)
            await uow.commit()
