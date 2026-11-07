from dependency_injector import containers, providers

from scribe.application.use_cases.auth import (
    GetUser,
    LoginUser,
    LogoutUser,
    RefreshTokens,
    RegisterUser,
)
from scribe.config import Settings
from scribe.infrastructure.db.session import create_engine, create_session_factory
from scribe.infrastructure.db.uow import SqlAlchemyUnitOfWork
from scribe.infrastructure.logging_config import configure_logging
from scribe.infrastructure.redis_client import create_redis
from scribe.infrastructure.security.passwords import Argon2PasswordHasher
from scribe.infrastructure.security.refresh_store import RedisRefreshTokenStore
from scribe.infrastructure.security.tokens import JwtTokenService


class Container(containers.DeclarativeContainer):
    wiring_config = containers.WiringConfiguration(packages=["scribe.presentation"])

    settings = providers.Singleton(Settings)

    # ── Resources ──
    logging = providers.Resource(
        configure_logging,
        level=settings.provided.log_level,
        json_logs=settings.provided.log_json,
    )

    # ── Clients ──
    engine = providers.Singleton(create_engine, url=settings.provided.database_url)
    redis = providers.Singleton(create_redis, url=settings.provided.redis_url)

    session_factory = providers.Singleton(create_session_factory, engine=engine)

    # ── Infrastructure ──
    uow = providers.Factory(SqlAlchemyUnitOfWork, session_factory=session_factory)
    password_hasher = providers.Singleton(Argon2PasswordHasher)
    token_service = providers.Singleton(
        JwtTokenService,
        secret=settings.provided.jwt_secret,
        algorithm=settings.provided.jwt_algorithm,
        access_ttl_min=settings.provided.access_token_ttl_min,
        refresh_ttl_days=settings.provided.refresh_token_ttl_days,
    )
    refresh_store = providers.Singleton(RedisRefreshTokenStore, redis=redis)

    # ── Use cases ──
    register_user = providers.Factory(
        RegisterUser, uow_factory=uow.provider, hasher=password_hasher
    )
    login_user = providers.Factory(
        LoginUser,
        uow_factory=uow.provider,
        hasher=password_hasher,
        tokens=token_service,
        store=refresh_store,
    )
    refresh_tokens = providers.Factory(
        RefreshTokens,
        uow_factory=uow.provider,
        tokens=token_service,
        store=refresh_store,
    )
    logout_user = providers.Factory(LogoutUser, tokens=token_service, store=refresh_store)
    get_user = providers.Factory(GetUser, uow_factory=uow.provider)
