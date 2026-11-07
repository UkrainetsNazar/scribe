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
    minio_bucket_uploads: str = "uploads"
    minio_bucket_artifacts: str = "artifacts"
    minio_bucket_exports: str = "exports"

    # Security
    jwt_secret: SecretStr = Field(default=SecretStr("dev-only-secret-change-me-in-prod-0123456789"))
    jwt_algorithm: str = "HS256"
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 7
    presigned_url_ttl_min: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
