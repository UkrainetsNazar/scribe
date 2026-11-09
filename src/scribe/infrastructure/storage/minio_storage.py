import asyncio
from collections.abc import Sequence
from datetime import timedelta

from minio import Minio
from minio.error import S3Error

from scribe.application.interfaces import ObjectStorage, StoredObject
from scribe.config import Settings

_PUBLIC_REGION = "us-east-1"
_MISSING_CODES = {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}


class MinioObjectStorage(ObjectStorage):
    def __init__(self, internal: Minio, public: Minio, buckets: Sequence[str]) -> None:
        self._internal = internal
        self._public = public
        self._buckets = tuple(buckets)

    async def ensure_buckets(self) -> None:
        await asyncio.to_thread(self._ensure_buckets)

    def _ensure_buckets(self) -> None:
        for bucket in self._buckets:
            if not self._internal.bucket_exists(bucket):
                self._internal.make_bucket(bucket)

    async def create_upload_url(self, bucket: str, key: str, ttl_seconds: int) -> str:
        return await asyncio.to_thread(
            self._public.presigned_put_object, bucket, key, timedelta(seconds=ttl_seconds)
        )

    async def create_internal_read_url(self, bucket: str, key: str, ttl_seconds: int) -> str:
        return await asyncio.to_thread(
            self._internal.presigned_get_object, bucket, key, timedelta(seconds=ttl_seconds)
        )

    async def stat(self, bucket: str, key: str) -> StoredObject | None:
        return await asyncio.to_thread(self._stat, bucket, key)

    def _stat(self, bucket: str, key: str) -> StoredObject | None:
        try:
            info = self._internal.stat_object(bucket, key)
        except S3Error as exc:
            if exc.code in _MISSING_CODES:
                return None
            raise
        return StoredObject(size=info.size or 0, content_type=info.content_type)


def create_object_storage(settings: Settings) -> MinioObjectStorage:
    secret = settings.minio_secret_key.get_secret_value()
    internal = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=secret,
        secure=settings.minio_secure,
    )
    public = Minio(
        settings.minio_public_endpoint,
        access_key=settings.minio_access_key,
        secret_key=secret,
        secure=settings.minio_public_secure,
        region=_PUBLIC_REGION,
    )
    return MinioObjectStorage(
        internal,
        public,
        buckets=[
            settings.minio_bucket_uploads,
            settings.minio_bucket_artifacts,
            settings.minio_bucket_exports,
        ],
    )
