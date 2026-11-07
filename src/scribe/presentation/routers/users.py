from fastapi import APIRouter, Depends

from scribe.domain.users import User
from scribe.presentation.deps import get_current_user
from scribe.presentation.schemas.auth import MeResponse

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse.from_domain(user)
