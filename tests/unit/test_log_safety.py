import logging

from scribe.infrastructure.logging_config import configure_logging
from scribe.infrastructure.media.ffprobe import _redact


def test_presigned_urls_are_redacted_from_ffprobe_errors() -> None:
    stderr = (
        "[http @ 0x1] HTTP error 404 for "
        "http://minio:9000/artifacts/o/r/source/original?X-Amz-Signature=secret&X-Amz-Credential=key\n"
        "Error opening input"
    )
    redacted = _redact(stderr)
    assert "secret" not in redacted and "X-Amz" not in redacted
    assert "<url>" in redacted and "Error opening input" in redacted


def test_http_libraries_do_not_log_request_urls() -> None:
    configure_logging("INFO", json_logs=False)
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
