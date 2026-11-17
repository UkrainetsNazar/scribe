import hashlib
import hmac
import json
import secrets
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from scribe.domain.errors import DomainValidationError

MAX_URL_LENGTH = 2048
SIGNATURE_HEADER = "X-Scribe-Signature"
DEFAULT_TOLERANCE_SEC = 300


def generate_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def validate_webhook_url(url: str) -> str:
    url = url.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise DomainValidationError(f"Webhook URL must be 1-{MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError as exc:
        raise DomainValidationError("Webhook URL is not valid") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DomainValidationError("Webhook URL must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise DomainValidationError("Webhook URL must not contain credentials")
    return url


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 over `<timestamp>.<raw body>`: the timestamp is signed too, so it cannot be
    replaced to replay an old request."""
    signed = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def signature_header(secret: str, timestamp: int, body: bytes) -> str:
    return f"t={timestamp},v1={sign(secret, timestamp, body)}"


def verify_signature(
    secret: str,
    header: str,
    body: bytes,
    *,
    now: float,
    tolerance_sec: int = DEFAULT_TOLERANCE_SEC,
) -> bool:
    """For receivers (and our tests): checks the signature and that the request is fresh."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        candidate = parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs(now - timestamp) > tolerance_sec:
        return False
    return hmac.compare_digest(candidate, sign(secret, timestamp, body))


def webhook_body(event_id: UUID, event_type: str, created_at: str, data: dict[str, Any]) -> bytes:
    """Deterministic JSON: the same event always yields the same bytes."""
    payload = {"id": str(event_id), "type": event_type, "created_at": created_at, "data": data}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
