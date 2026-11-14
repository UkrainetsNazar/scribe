from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Response, status
from fastapi.security import HTTPAuthorizationCredentials

from scribe.application.interfaces import TokenService
from scribe.container import Container
from scribe.domain.errors import UnauthorizedError
from scribe.presentation.deps import bearer_scheme

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


@router.get("/auth", status_code=status.HTTP_204_NO_CONTENT)
@inject
async def forward_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    tokens: TokenService = Depends(Provide[Container.token_service]),
) -> Response:
    """Called by the gateway for every protected request.

    A cheap stateless check: no database. Inactive or deleted users are still rejected
    by the endpoints themselves. On failure the client receives this problem+json response.
    """
    if credentials is None:
        raise UnauthorizedError
    tokens.decode(credentials.credentials, "access")  # raises 401 / TOKEN_EXPIRED
    return Response(status_code=status.HTTP_204_NO_CONTENT)
