from dataclasses import dataclass
from uuid import UUID

from scribe.application.interfaces import (
    PasswordHasher,
    RefreshTokenStore,
    TokenService,
    UnitOfWorkFactory,
)
from scribe.domain.errors import EmailTakenError, UnauthorizedError
from scribe.domain.users import User, normalize_email

DEFAULT_PLAN = "free"


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


async def _issue_pair(tokens: TokenService, store: RefreshTokenStore, user_id: UUID) -> TokenPair:
    access = tokens.create_access_token(user_id)
    refresh = tokens.create_refresh_token(user_id)
    await store.save(refresh.jti, user_id, refresh.ttl_seconds)
    return TokenPair(
        access_token=access.token,
        refresh_token=refresh.token,
        expires_in=access.ttl_seconds,
    )


class RegisterUser:
    def __init__(self, uow_factory: UnitOfWorkFactory, hasher: PasswordHasher) -> None:
        self._uow_factory = uow_factory
        self._hasher = hasher

    async def execute(self, email: str, password: str) -> User:
        email = normalize_email(email)
        password_hash = await self._hasher.hash(password)
        async with self._uow_factory() as uow:
            if await uow.users.get_by_email(email) is not None:
                raise EmailTakenError
            plan = await uow.plans.get_by_name(DEFAULT_PLAN)
            if plan is None:
                raise RuntimeError(f"Default plan '{DEFAULT_PLAN}' is not seeded")
            user = User.register(email, password_hash, plan)
            await uow.users.add(user)
            await uow.commit()
        return user


class LoginUser:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        hasher: PasswordHasher,
        tokens: TokenService,
        store: RefreshTokenStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._hasher = hasher
        self._tokens = tokens
        self._store = store

    async def execute(self, email: str, password: str) -> TokenPair:
        async with self._uow_factory() as uow:
            user = await uow.users.get_by_email(normalize_email(email))

        if user is None:
            await self._hasher.verify(password, self._hasher.dummy_hash)
            raise UnauthorizedError("Invalid email or password")
        if not await self._hasher.verify(password, user.password_hash) or not user.is_active:
            raise UnauthorizedError("Invalid email or password")

        return await _issue_pair(self._tokens, self._store, user.id)


class RefreshTokens:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        tokens: TokenService,
        store: RefreshTokenStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._tokens = tokens
        self._store = store

    async def execute(self, refresh_token: str) -> TokenPair:
        claims = self._tokens.decode(refresh_token, "refresh")
        owner_id = await self._store.consume(claims.jti)
        if owner_id is None or owner_id != claims.sub:
            raise UnauthorizedError("Refresh token is revoked or already used")

        async with self._uow_factory() as uow:
            user = await uow.users.get_by_id(claims.sub)
        if user is None or not user.is_active:
            raise UnauthorizedError("User is not available")

        return await _issue_pair(self._tokens, self._store, user.id)


class LogoutUser:
    def __init__(self, tokens: TokenService, store: RefreshTokenStore) -> None:
        self._tokens = tokens
        self._store = store

    async def execute(self, refresh_token: str) -> None:
        try:
            claims = self._tokens.decode(refresh_token, "refresh")
        except UnauthorizedError:
            return
        await self._store.revoke(claims.jti)


class GetUser:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, user_id: UUID) -> User:
        async with self._uow_factory() as uow:
            user = await uow.users.get_by_id(user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("User is not available")
        return user
