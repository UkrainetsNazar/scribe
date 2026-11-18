from uuid import uuid4

import pytest

from scribe.application.interfaces import UnitOfWorkFactory
from scribe.application.use_cases.projects import (
    CreateProject,
    DeleteProject,
    GetProject,
    ListProjects,
    UpdateProject,
)
from scribe.domain.errors import (
    BadRequestError,
    DomainValidationError,
    NotFoundError,
    ProjectNameTakenError,
)

OWNER = uuid4()


async def test_create_trims_the_name_and_rejects_duplicates(uow_factory: UnitOfWorkFactory) -> None:
    create = CreateProject(uow_factory)
    project = await create.execute(OWNER, "  Podcasts ", "weekly")
    assert (project.name, project.description) == ("Podcasts", "weekly")
    with pytest.raises(ProjectNameTakenError):
        await create.execute(OWNER, "Podcasts", None)
    assert (await create.execute(uuid4(), "Podcasts", None)).name == "Podcasts"  # another owner


async def test_get_is_scoped_to_the_owner(uow_factory: UnitOfWorkFactory) -> None:
    project = await CreateProject(uow_factory).execute(OWNER, "P", None)
    assert (await GetProject(uow_factory).execute(OWNER, project.id)).id == project.id
    with pytest.raises(NotFoundError):
        await GetProject(uow_factory).execute(uuid4(), project.id)


async def test_update_changes_only_the_given_fields(uow_factory: UnitOfWorkFactory) -> None:
    project = await CreateProject(uow_factory).execute(OWNER, "P", "keep")
    update = UpdateProject(uow_factory)
    renamed = await update.execute(OWNER, project.id, {"name": "Q"})
    assert (renamed.name, renamed.description) == ("Q", "keep")
    assert (await update.execute(OWNER, project.id, {"description": None})).description is None


async def test_update_rejects_taken_and_blank_names(uow_factory: UnitOfWorkFactory) -> None:
    create, update = CreateProject(uow_factory), UpdateProject(uow_factory)
    await create.execute(OWNER, "A", None)
    second = await create.execute(OWNER, "B", None)
    with pytest.raises(ProjectNameTakenError):
        await update.execute(OWNER, second.id, {"name": "A"})
    with pytest.raises(DomainValidationError):
        await update.execute(OWNER, second.id, {"name": "   "})
    with pytest.raises(NotFoundError):
        await update.execute(OWNER, uuid4(), {"name": "X"})


async def test_delete_hides_the_project_and_frees_the_name(uow_factory: UnitOfWorkFactory) -> None:
    create = CreateProject(uow_factory)
    project = await create.execute(OWNER, "P", None)
    await DeleteProject(uow_factory).execute(OWNER, project.id)
    with pytest.raises(NotFoundError):
        await GetProject(uow_factory).execute(OWNER, project.id)
    with pytest.raises(NotFoundError):
        await DeleteProject(uow_factory).execute(OWNER, project.id)
    assert (await create.execute(OWNER, "P", None)).id != project.id


async def test_list_pages_newest_first(uow_factory: UnitOfWorkFactory) -> None:
    for name in ("a", "b", "c"):
        await CreateProject(uow_factory).execute(OWNER, name, None)
    listing = ListProjects(uow_factory)

    page = await listing.execute(OWNER, 2, None)
    assert [p.name for p in page.items] == ["c", "b"] and page.next_cursor
    rest = await listing.execute(OWNER, 2, page.next_cursor)
    assert [p.name for p in rest.items] == ["a"] and rest.next_cursor is None
    with pytest.raises(BadRequestError):
        await listing.execute(OWNER, 2, "garbage")
