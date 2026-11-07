import pytest
from pydantic import SecretStr

from scribe.domain.errors import TokenExpiredError, UnauthorizedError
from scribe.infrastructure.security.passwords import Argon2PasswordHasher
from scribe.infrastructure.security.tokens import JwtTokenService

SECRET = SecretStr("unit-test-secret-0123456789-0123456789")


def make_service(access_ttl_min: int = 15) -> JwtTokenService:
    return JwtTokenService(SECRET, "HS256", access_ttl_min=access_ttl_min, refresh_ttl_days=7)


async def test_password_hash_and_verify() -> None:
    hasher = Argon2PasswordHasher()
    hashed = await hasher.hash("correct-horse-1")
    assert hashed != "correct-horse-1"
    assert await hasher.verify("correct-horse-1", hashed) is True
    assert await hasher.verify("wrong-password-1", hashed) is False
    assert await hasher.verify("x", "not-a-hash") is False


def test_token_roundtrip() -> None:
    from uuid import uuid4

    service = make_service()
    user_id = uuid4()
    issued = service.create_access_token(user_id)
    claims = service.decode(issued.token, "access")
    assert claims.sub == user_id
    assert claims.jti == issued.jti
    assert issued.ttl_seconds == 900


def test_expired_token() -> None:
    from uuid import uuid4

    service = make_service(access_ttl_min=-1)
    token = service.create_access_token(uuid4()).token
    with pytest.raises(TokenExpiredError):
        service.decode(token, "access")


def test_wrong_token_type_is_rejected() -> None:
    from uuid import uuid4

    service = make_service()
    refresh = service.create_refresh_token(uuid4()).token
    with pytest.raises(UnauthorizedError):
        service.decode(refresh, "access")


def test_tampered_token_is_rejected() -> None:
    from uuid import uuid4

    service = make_service()
    token = service.create_access_token(uuid4()).token
    with pytest.raises(UnauthorizedError):
        service.decode(token[:-3] + "abc", "access")
