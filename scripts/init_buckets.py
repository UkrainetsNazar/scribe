from minio import Minio

from scribe.config import get_settings


def main() -> None:
    s = get_settings()
    client = Minio(
        s.minio_endpoint,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key.get_secret_value(),
        secure=s.minio_secure,
    )
    for bucket in (s.minio_bucket_uploads, s.minio_bucket_artifacts, s.minio_bucket_exports):
        if client.bucket_exists(bucket):
            print(f"exists:  {bucket}")
        else:
            client.make_bucket(bucket)
            print(f"created: {bucket}")


if __name__ == "__main__":
    main()
