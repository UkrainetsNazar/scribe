from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Response, status

from scribe.application.use_cases.auth import LoginUser, LogoutUser, RefreshTokens, RegisterUser
from scribe.container import Container
from scribe.presentation.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPairResponse,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
@inject
async def register(
    body: RegisterRequest,
    use_case: RegisterUser = Depends(Provide[Container.register_user]),
) -> UserResponse:
    user = await use_case.execute(body.email, body.password)
    return UserResponse.from_domain(user)


@router.post("/login")
@inject
async def login(
    body: LoginRequest,
    use_case: LoginUser = Depends(Provide[Container.login_user]),
) -> TokenPairResponse:
    return TokenPairResponse.from_domain(await use_case.execute(body.email, body.password))


@router.post("/refresh")
@inject
async def refresh(
    body: RefreshRequest,
    use_case: RefreshTokens = Depends(Provide[Container.refresh_tokens]),
) -> TokenPairResponse:
    return TokenPairResponse.from_domain(await use_case.execute(body.refresh_token))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@inject
async def logout(
    body: RefreshRequest,
    use_case: LogoutUser = Depends(Provide[Container.logout_user]),
) -> Response:
    await use_case.execute(body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
