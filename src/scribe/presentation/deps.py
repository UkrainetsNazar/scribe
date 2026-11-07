from dependency_injector.wiring import Provide, inject
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from scribe.application.interfaces import TokenService
from scribe.application.use_cases.auth import GetUser
from scribe.container import Container
from scribe.domain.errors import UnauthorizedError
from scribe.domain.users import User

bearer_scheme = HTTPBearer(auto_error=False)


@inject
async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    tokens: TokenService = Depends(Provide[Container.token_service]),
    get_user: GetUser = Depends(Provide[Container.get_user]),
) -> User:
    if credentials is None:
        raise UnauthorizedError
    claims = tokens.decode(credentials.credentials, "access")
    return await get_user.execute(claims.sub)
