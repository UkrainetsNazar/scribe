from dependency_injector import containers, providers

from scribe.application.pipeline import StageRunner
from scribe.application.stage_handlers import (
    BuildSubtitlesHandler,
    ExtractAudioHandler,
    IndexHandler,
    NormalizeAndChunkHandler,
    SummarizeHandler,
    TranscribeHandler,
)
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
    CancelRecording,
    CompleteUpload,
    CreateRecording,
    DeleteRecording,
    GetRecording,
    ListRecordings,
    RetryRecording,
)
from scribe.application.use_cases.usage import GetMonthlyUsage
from scribe.config import Settings
from scribe.infrastructure.celery_dispatcher import CeleryPipelineDispatcher
from scribe.infrastructure.db.session import create_engine, create_session_factory
from scribe.infrastructure.db.uow import SqlAlchemyUnitOfWork
from scribe.infrastructure.logging_config import configure_logging
from scribe.infrastructure.media.ffmpeg import FfmpegAudioProcessor
from scribe.infrastructure.media.ffprobe import FfprobeMediaProbe
from scribe.infrastructure.messaging.publisher import RabbitMqPublisher
from scribe.infrastructure.messaging.relay import OutboxRelay
from scribe.infrastructure.mongo import MongoTranscriptIndex, create_mongo_client, get_database
from scribe.infrastructure.redis_client import create_redis
from scribe.infrastructure.security.passwords import Argon2PasswordHasher
from scribe.infrastructure.security.refresh_store import RedisRefreshTokenStore
from scribe.infrastructure.security.tokens import JwtTokenService
from scribe.infrastructure.storage.minio_storage import create_object_storage
from scribe.infrastructure.summarization.extractive import ExtractiveSummarizer
from scribe.infrastructure.transcription.stub import StubTranscriber
from scribe.infrastructure.transcription.whisper import FasterWhisperTranscriber


class Container(containers.DeclarativeContainer):
    # Wiring is done explicitly by create_app(); workers create many containers
    wiring_config = containers.WiringConfiguration(
        packages=["scribe.presentation"], auto_wire=False
    )

    settings = providers.Singleton(Settings)

    # ── Resources ──
    logging = providers.Resource(
        configure_logging,
        level=settings.provided.log_level,
        json_logs=settings.provided.log_json,
    )

    # ── Clients (closed manually by the owning process) ──
    engine = providers.Singleton(create_engine, url=settings.provided.database_url)
    redis = providers.Singleton(create_redis, url=settings.provided.redis_url)
    mongo = providers.Singleton(create_mongo_client, url=settings.provided.mongo_url)
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
    message_publisher = providers.Singleton(
        RabbitMqPublisher,
        url=settings.provided.rabbitmq_url,
        retry_delays=settings.provided.rabbitmq_retry_delays_sec,
    )
    outbox_relay = providers.Singleton(
        OutboxRelay,
        session_factory=session_factory,
        publisher=message_publisher,
        batch_size=settings.provided.outbox_batch_size,
        poll_interval_sec=settings.provided.outbox_poll_interval_sec,
    )

    # ── Pipeline ──
    pipeline_dispatcher = providers.Singleton(CeleryPipelineDispatcher)
    mongo_db = providers.Singleton(get_database, client=mongo, name=settings.provided.mongo_db)
    transcript_index = providers.Singleton(MongoTranscriptIndex, db=mongo_db)
    audio_processor = providers.Singleton(
        FfmpegAudioProcessor,
        binary=settings.provided.ffmpeg_bin,
        timeout_sec=settings.provided.ffmpeg_timeout_sec,
    )
    transcriber = providers.Selector(
        settings.provided.transcriber,
        whisper=providers.Singleton(
            FasterWhisperTranscriber,
            model_size=settings.provided.whisper_model,
            device=settings.provided.whisper_device,
            compute_type=settings.provided.whisper_compute_type,
            beam_size=settings.provided.whisper_beam_size,
        ),
        stub=providers.Singleton(StubTranscriber),
    )
    summarizer = providers.Singleton(ExtractiveSummarizer)

    extract_audio_handler = providers.Factory(
        ExtractAudioHandler,
        storage=object_storage,
        audio=audio_processor,
        uploads_bucket=settings.provided.minio_bucket_uploads,
        artifacts_bucket=settings.provided.minio_bucket_artifacts,
    )
    normalize_handler = providers.Factory(
        NormalizeAndChunkHandler,
        storage=object_storage,
        audio=audio_processor,
        artifacts_bucket=settings.provided.minio_bucket_artifacts,
        chunk_seconds=settings.provided.chunk_seconds,
    )
    transcribe_handler = providers.Factory(
        TranscribeHandler,
        storage=object_storage,
        transcriber=transcriber,
        artifacts_bucket=settings.provided.minio_bucket_artifacts,
    )
    subtitles_handler = providers.Factory(
        BuildSubtitlesHandler,
        storage=object_storage,
        artifacts_bucket=settings.provided.minio_bucket_artifacts,
    )
    summarize_handler = providers.Factory(
        SummarizeHandler,
        storage=object_storage,
        summarizer=summarizer,
        artifacts_bucket=settings.provided.minio_bucket_artifacts,
    )
    index_handler = providers.Factory(
        IndexHandler,
        storage=object_storage,
        index=transcript_index,
        artifacts_bucket=settings.provided.minio_bucket_artifacts,
    )
    stage_runner = providers.Factory(
        StageRunner,
        uow_factory=uow.provider,
        handlers=providers.List(
            extract_audio_handler,
            normalize_handler,
            transcribe_handler,
            subtitles_handler,
            summarize_handler,
            index_handler,
        ),
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
    get_monthly_usage = providers.Factory(GetMonthlyUsage, uow_factory=uow.provider)

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
    retry_recording = providers.Factory(RetryRecording, uow_factory=uow.provider)
    cancel_recording = providers.Factory(CancelRecording, uow_factory=uow.provider)
    get_recording = providers.Factory(GetRecording, uow_factory=uow.provider)
    list_recordings = providers.Factory(ListRecordings, uow_factory=uow.provider)
    delete_recording = providers.Factory(DeleteRecording, uow_factory=uow.provider)
