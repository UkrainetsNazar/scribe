import asyncio
import io
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from minio import Minio
from minio.commonconfig import CopySource
from minio.deleteobjects import DeleteObject
from minio.error import S3Error

from scribe.application.interfaces import ObjectModifiedError, ObjectStorage, StoredObject
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
        return StoredObject(
            size=info.size or 0, content_type=info.content_type, etag=info.etag or ""
        )

    async def copy(
        self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str, *, expected_etag: str
    ) -> None:
        await asyncio.to_thread(self._copy, src_bucket, src_key, dst_bucket, dst_key, expected_etag)

    def _copy(
        self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str, expected_etag: str
    ) -> None:
        source = CopySource(src_bucket, src_key, match_etag=expected_etag)
        try:
            self._internal.copy_object(dst_bucket, dst_key, source)
        except S3Error as exc:
            if exc.code == "PreconditionFailed":
                raise ObjectModifiedError(src_key) from exc
            if exc.code in _MISSING_CODES:
                raise FileNotFoundError(src_key) from exc
            raise

    async def upload_file(self, bucket: str, key: str, path: Path, content_type: str) -> None:
        await asyncio.to_thread(
            self._internal.fput_object, bucket, key, str(path), content_type=content_type
        )

    async def download_file(self, bucket: str, key: str, path: Path) -> None:
        await asyncio.to_thread(self._download_file, bucket, key, path)

    def _download_file(self, bucket: str, key: str, path: Path) -> None:
        try:
            self._internal.fget_object(bucket, key, str(path))
        except S3Error as exc:
            if exc.code in _MISSING_CODES:
                raise FileNotFoundError(key) from exc
            raise

    async def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._internal.put_object,
            bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
        )

    async def get_bytes(self, bucket: str, key: str) -> bytes | None:
        return await asyncio.to_thread(self._get_bytes, bucket, key)

    def _get_bytes(self, bucket: str, key: str) -> bytes | None:
        try:
            response = self._internal.get_object(bucket, key)
        except S3Error as exc:
            if exc.code in _MISSING_CODES:
                return None
            raise
        try:
            return bytes(response.read())
        finally:
            response.close()
            response.release_conn()

    async def create_download_url(
        self, bucket: str, key: str, ttl_seconds: int, filename: str
    ) -> str:
        return await asyncio.to_thread(
            self._public.presigned_get_object,
            bucket,
            key,
            timedelta(seconds=ttl_seconds),
            response_headers={"response-content-disposition": f'attachment; filename="{filename}"'},
        )

    async def ping(self) -> None:
        """Raises if MinIO is unreachable or rejects the credentials."""
        await asyncio.to_thread(self._internal.bucket_exists, self._buckets[0])

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        return await asyncio.to_thread(self._delete_prefix, bucket, prefix)

    def _delete_prefix(self, bucket: str, prefix: str) -> int:
        try:
            names = [
                obj.object_name
                for obj in self._internal.list_objects(bucket, prefix=prefix, recursive=True)
                if obj.object_name
            ]
        except S3Error as exc:
            if exc.code in _MISSING_CODES:
                return 0
            raise
        # remove_objects is lazy: the errors must be consumed for the deletion to happen
        errors = list(self._internal.remove_objects(bucket, [DeleteObject(n) for n in names]))
        if errors:
            raise RuntimeError(f"Could not delete {len(errors)} object(s) under '{prefix}'")
        return len(names)


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
