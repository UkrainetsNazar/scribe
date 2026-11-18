import asyncio

import pytest
from minio import Minio

from scribe.config import Settings
from scribe.infrastructure.storage.minio_storage import UPLOAD_RETENTION_DAYS, create_object_storage

pytestmark = pytest.mark.integration


async def test_uploads_expire_but_artifacts_do_not(app_settings: Settings) -> None:
    storage = create_object_storage(app_settings)
    await storage.ensure_buckets()
    await storage.ensure_buckets()  # idempotent

    client = Minio(
        app_settings.minio_endpoint,
        access_key=app_settings.minio_access_key,
        secret_key=app_settings.minio_secret_key.get_secret_value(),
        secure=False,
    )
    config = await asyncio.to_thread(client.get_bucket_lifecycle, app_settings.minio_bucket_uploads)
    assert config is not None
    assert config.rules[0].expiration.days == UPLOAD_RETENTION_DAYS
    assert await asyncio.to_thread(client.get_bucket_lifecycle, "artifacts") is None
