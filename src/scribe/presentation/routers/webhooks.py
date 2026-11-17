from uuid import UUID

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Response, status

from scribe.application.use_cases.webhooks import (
    ConfigureWebhook,
    DeleteWebhook,
    GetWebhook,
    RotateWebhookSecret,
)
from scribe.container import Container
from scribe.domain.users import User
from scribe.presentation.deps import get_current_user
from scribe.presentation.schemas.webhooks import (
    WebhookRequest,
    WebhookResponse,
    WebhookSecretResponse,
)

router = APIRouter(prefix="/projects/{project_id}/webhook", tags=["webhooks"])


@router.put("")
@inject
async def configure_webhook(
    project_id: UUID,
    body: WebhookRequest,
    user: User = Depends(get_current_user),
    use_case: ConfigureWebhook = Depends(Provide[Container.configure_webhook]),
) -> WebhookSecretResponse:
    return WebhookSecretResponse.from_domain(await use_case.execute(user.id, project_id, body.url))


@router.get("")
@inject
async def read_webhook(
    project_id: UUID,
    user: User = Depends(get_current_user),
    use_case: GetWebhook = Depends(Provide[Container.get_webhook]),
) -> WebhookResponse:
    return WebhookResponse.from_domain(await use_case.execute(user.id, project_id))


@router.post("/rotate-secret")
@inject
async def rotate_webhook_secret(
    project_id: UUID,
    user: User = Depends(get_current_user),
    use_case: RotateWebhookSecret = Depends(Provide[Container.rotate_webhook_secret]),
) -> WebhookSecretResponse:
    return WebhookSecretResponse.from_domain(await use_case.execute(user.id, project_id))


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
@inject
async def remove_webhook(
    project_id: UUID,
    user: User = Depends(get_current_user),
    use_case: DeleteWebhook = Depends(Provide[Container.delete_webhook]),
) -> Response:
    await use_case.execute(user.id, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
