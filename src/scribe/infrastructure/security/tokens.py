from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from pydantic import SecretStr

from scribe.application.interfaces import IssuedToken, TokenClaims, TokenService, TokenType
from scribe.domain.errors import TokenExpiredError, UnauthorizedError


class JwtTokenService(TokenService):
    def __init__(
        self,
        secret: SecretStr,
        algorithm: str,
        access_ttl_min: int,
        refresh_ttl_days: int,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._access_ttl = timedelta(minutes=access_ttl_min)
        self._refresh_ttl = timedelta(days=refresh_ttl_days)

    def create_access_token(self, user_id: UUID) -> IssuedToken:
        return self._create(user_id, "access", self._access_ttl)

    def create_refresh_token(self, user_id: UUID) -> IssuedToken:
        return self._create(user_id, "refresh", self._refresh_ttl)

    def _create(self, user_id: UUID, token_type: TokenType, ttl: timedelta) -> IssuedToken:
        now = datetime.now(UTC)
        jti = uuid4().hex
        payload = {
            "sub": str(user_id),
            "jti": jti,
            "type": token_type,
            "iat": now,
            "exp": now + ttl,
        }
        token = jwt.encode(payload, self._secret.get_secret_value(), algorithm=self._algorithm)
        return IssuedToken(token=token, jti=jti, ttl_seconds=int(ttl.total_seconds()))

    def decode(self, token: str, expected: TokenType) -> TokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret.get_secret_value(),
                algorithms=[self._algorithm],
                options={"require": ["exp", "iat", "sub", "jti", "type"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError from exc
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("Invalid token") from exc

        if payload["type"] != expected:
            raise UnauthorizedError("Invalid token type")
        try:
            sub = UUID(str(payload["sub"]))
        except ValueError as exc:
            raise UnauthorizedError("Invalid token") from exc

        return TokenClaims(
            sub=sub,
            jti=str(payload["jti"]),
            type=expected,
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        )
