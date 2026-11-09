from dependency_injector import containers, providers

from scribe.application.use_cases.auth import (
    GetUser,
    LoginUser,
    LogoutUser,
    RefreshTokens,
    RegisterUser,
)
from scribe.application.use_cases.projects import (
    CreateProject,
    DeleteProject,
    GetProject,
    ListProjects,
    UpdateProject,
)
from scribe.application.use_cases.recordings import (
    CompleteUpload,
    CreateRecording,
    DeleteRecording,
    GetRecording,
    ListRecordings,
)
from scribe.config import Settings
from scribe.infrastructure.db.session import create_engine, create_session_factory
from scribe.infrastructure.db.uow import SqlAlchemyUnitOfWork
from scribe.infrastructure.logging_config import configure_logging
from scribe.infrastructure.media.ffprobe import FfprobeMediaProbe
from scribe.infrastructure.redis_client import create_redis
from scribe.infrastructure.security.passwords import Argon2PasswordHasher
from scribe.infrastructure.security.refresh_store import RedisRefreshTokenStore
from scribe.infrastructure.security.tokens import JwtTokenService
from scribe.infrastructure.storage.minio_storage import create_object_storage


class Container(containers.DeclarativeContainer):
    wiring_config = containers.WiringConfiguration(packages=["scribe.presentation"])

    settings = providers.Singleton(Settings)

    # ── Resources
    logging = providers.Resource(
        configure_logging,
        level=settings.provided.log_level,
        json_logs=settings.provided.log_json,
    )

    # ── Clients (closed manually in the app lifespan) ──
    engine = providers.Singleton(create_engine, url=settings.provided.database_url)
    redis = providers.Singleton(create_redis, url=settings.provided.redis_url)
    session_factory = providers.Singleton(create_session_factory, engine=engine)
    object_storage = providers.Singleton(create_object_storage, settings=settings)

    # ── Infrastructure ──
    # uow.provider passes the factory itself into the use case: each call creates a new UoW
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
    media_probe = providers.Singleton(
        FfprobeMediaProbe,
        binary=settings.provided.ffprobe_bin,
        timeout_sec=settings.provided.ffprobe_timeout_sec,
    )

    # ── Use cases: auth ──
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

    # ── Use cases: projects ──
    create_project = providers.Factory(CreateProject, uow_factory=uow.provider)
    list_projects = providers.Factory(ListProjects, uow_factory=uow.provider)
    get_project = providers.Factory(GetProject, uow_factory=uow.provider)
    update_project = providers.Factory(UpdateProject, uow_factory=uow.provider)
    delete_project = providers.Factory(DeleteProject, uow_factory=uow.provider)

    # ── Use cases: recordings ──
    create_recording = providers.Factory(
        CreateRecording,
        uow_factory=uow.provider,
        storage=object_storage,
        bucket=settings.provided.minio_bucket_uploads,
        upload_ttl_min=settings.provided.presigned_url_ttl_min,
    )
    complete_upload = providers.Factory(
        CompleteUpload,
        uow_factory=uow.provider,
        storage=object_storage,
        probe=media_probe,
        bucket=settings.provided.minio_bucket_uploads,
    )
    get_recording = providers.Factory(GetRecording, uow_factory=uow.provider)
    list_recordings = providers.Factory(ListRecordings, uow_factory=uow.provider)
    delete_recording = providers.Factory(DeleteRecording, uow_factory=uow.provider)
