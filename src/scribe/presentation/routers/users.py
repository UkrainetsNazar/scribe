from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from scribe.application.use_cases.usage import GetMonthlyUsage
from scribe.container import Container
from scribe.domain.users import User
from scribe.presentation.deps import get_current_user
from scribe.presentation.schemas.auth import MeResponse

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
@inject
async def me(
    user: User = Depends(get_current_user),
    usage: GetMonthlyUsage = Depends(Provide[Container.get_monthly_usage]),
) -> MeResponse:
    return MeResponse.build(user, await usage.execute(user.id))
