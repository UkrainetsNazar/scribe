from dataclasses import dataclass
from uuid import UUID

from scribe.application.interfaces import UnitOfWork, UnitOfWorkFactory
from scribe.domain.errors import NotFoundError
from scribe.domain.projects import Project


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    url: str | None
    secret: str | None = None  # revealed only when it is set or rotated


async def _load(uow: UnitOfWork, owner_id: UUID, project_id: UUID) -> Project:
    project = await uow.projects.get(project_id, owner_id)
    if project is None:
        raise NotFoundError("Project not found")
    return project


class ConfigureWebhook:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, project_id: UUID, url: str) -> WebhookConfig:
        async with self._uow_factory() as uow:
            project = await _load(uow, owner_id, project_id)
            project.set_webhook(url)
            await uow.projects.save(project)
            await uow.commit()
        return WebhookConfig(project.webhook_url, project.webhook_secret)


class GetWebhook:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, project_id: UUID) -> WebhookConfig:
        async with self._uow_factory() as uow:
            project = await _load(uow, owner_id, project_id)
        return WebhookConfig(project.webhook_url)


class RotateWebhookSecret:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, project_id: UUID) -> WebhookConfig:
        async with self._uow_factory() as uow:
            project = await _load(uow, owner_id, project_id)
            project.rotate_webhook_secret()  # 409 when no webhook is configured
            await uow.projects.save(project)
            await uow.commit()
        return WebhookConfig(project.webhook_url, project.webhook_secret)


class DeleteWebhook:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, owner_id: UUID, project_id: UUID) -> None:
        async with self._uow_factory() as uow:
            project = await _load(uow, owner_id, project_id)
            project.clear_webhook()
            await uow.projects.save(project)
            await uow.commit()
