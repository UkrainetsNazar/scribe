from typing import Annotated
from uuid import UUID

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Query, Response, status

from scribe.application.use_cases.projects import (
    CreateProject,
    DeleteProject,
    GetProject,
    ListProjects,
    UpdateProject,
)
from scribe.container import Container
from scribe.domain.users import User
from scribe.presentation.deps import get_current_user
from scribe.presentation.schemas.projects import (
    ProjectCreateRequest,
    ProjectPageResponse,
    ProjectResponse,
    ProjectUpdateRequest,
)

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", status_code=status.HTTP_201_CREATED)
@inject
async def create_project(
    body: ProjectCreateRequest,
    user: User = Depends(get_current_user),
    use_case: CreateProject = Depends(Provide[Container.create_project]),
) -> ProjectResponse:
    project = await use_case.execute(user.id, body.name, body.description)
    return ProjectResponse.from_domain(project)


@router.get("")
@inject
async def list_projects(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    user: User = Depends(get_current_user),
    use_case: ListProjects = Depends(Provide[Container.list_projects]),
) -> ProjectPageResponse:
    page = await use_case.execute(user.id, limit, cursor)
    return ProjectPageResponse(
        items=[ProjectResponse.from_domain(p) for p in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{project_id}")
@inject
async def get_project(
    project_id: UUID,
    user: User = Depends(get_current_user),
    use_case: GetProject = Depends(Provide[Container.get_project]),
) -> ProjectResponse:
    return ProjectResponse.from_domain(await use_case.execute(user.id, project_id))


@router.patch("/{project_id}")
@inject
async def update_project(
    project_id: UUID,
    body: ProjectUpdateRequest,
    user: User = Depends(get_current_user),
    use_case: UpdateProject = Depends(Provide[Container.update_project]),
) -> ProjectResponse:
    changes = body.model_dump(exclude_unset=True)
    return ProjectResponse.from_domain(await use_case.execute(user.id, project_id, changes))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
@inject
async def delete_project(
    project_id: UUID,
    user: User = Depends(get_current_user),
    use_case: DeleteProject = Depends(Provide[Container.delete_project]),
) -> Response:
    await use_case.execute(user.id, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
