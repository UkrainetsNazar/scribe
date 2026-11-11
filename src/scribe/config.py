from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "scribe"
    app_env: Literal["local", "test", "prod"] = "local"
    log_level: str = "INFO"
    log_json: bool = True

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://scribe:scribe@localhost:5432/scribe"

    # MongoDB
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "scribe"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # RabbitMQ
    rabbitmq_url: str = "amqp://scribe:scribe@localhost:5672/"

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "scribe"
    minio_secret_key: SecretStr = SecretStr("scribe-secret")
    minio_secure: bool = False

    minio_public_endpoint: str = "localhost:9000"
    minio_public_secure: bool = False
    minio_bucket_uploads: str = "uploads"
    minio_bucket_artifacts: str = "artifacts"
    minio_bucket_exports: str = "exports"

    # Media probing
    ffprobe_bin: str = "ffprobe"
    ffprobe_timeout_sec: float = 30.0

    # Messaging and outbox relay
    rabbitmq_retry_delays_sec: tuple[int, ...] = (5, 30, 120, 600)
    rabbitmq_prefetch: int = 10
    outbox_batch_size: int = 100
    outbox_poll_interval_sec: float = 0.2

    # Pipeline
    transcriber: Literal["whisper", "stub"] = "whisper"
    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    whisper_beam_size: int = 5
    ffmpeg_bin: str = "ffmpeg"
    ffmpeg_timeout_sec: float = 3600.0
    chunk_seconds: int = 600
    pipeline_max_retries: int = 5

    # Security
    jwt_secret: SecretStr = Field(default=SecretStr("dev-only-secret-change-me-in-prod-0123456789"))
    jwt_algorithm: str = "HS256"
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 7
    presigned_url_ttl_min: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
