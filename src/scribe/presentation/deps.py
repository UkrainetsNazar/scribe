import structlog
from dependency_injector.wiring import Provide, inject
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from scribe.application.interfaces import RateLimiter, TokenService
from scribe.application.use_cases.auth import GetUser
from scribe.config import Settings
from scribe.container import Container
from scribe.domain.errors import RateLimitedError, UnauthorizedError
from scribe.domain.users import User
from scribe.presentation.http_metrics import handler_label

log = structlog.get_logger()

bearer_scheme = HTTPBearer(auto_error=False)

# Expensive operations get the stricter limit
HEAVY_ROUTES = frozenset(
    {
        ("POST", "/api/v1/recordings"),
        ("POST", "/api/v1/recordings/{id}/complete-upload"),
        ("POST", "/api/v1/recordings/{id}/exports"),
        ("GET", "/api/v1/search"),
    }
)


async def _enforce_rate_limit(
    request: Request, user: User, limiter: RateLimiter, settings: Settings
) -> None:
    route = handler_label(request.url.path, 200)  # ids collapsed: one counter per route
    heavy = (request.method, route) in HEAVY_ROUTES
    limit = settings.rate_limit_heavy_per_minute if heavy else settings.rate_limit_per_minute
    try:
        decision = await limiter.hit(
            f"rl:{user.id}:{request.method} {route}", limit, settings.rate_limit_window_sec
        )
    except Exception:
        # Fail open: an unavailable Redis must not take the API down
        log.warning("rate_limit.unavailable", exc_info=True)
        return
    if not decision.allowed:
        raise RateLimitedError(decision.retry_after_sec)


@inject
async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    tokens: TokenService = Depends(Provide[Container.token_service]),
    get_user: GetUser = Depends(Provide[Container.get_user]),
    limiter: RateLimiter = Depends(Provide[Container.rate_limiter]),
    settings: Settings = Depends(Provide[Container.settings]),
) -> User:
    if credentials is None:
        raise UnauthorizedError
    claims = tokens.decode(credentials.credentials, "access")
    user = await get_user.execute(claims.sub)
    await _enforce_rate_limit(request, user, limiter, settings)
    return user
