from uuid import UUID

import pytest
from pydantic import SecretStr

from scribe.application.interfaces import UnitOfWork, UnitOfWorkFactory
from scribe.application.use_cases.auth import LoginUser, LogoutUser, RefreshTokens, RegisterUser
from scribe.domain.errors import EmailTakenError, UnauthorizedError
from scribe.domain.users import Plan, User
from scribe.infrastructure.security.tokens import JwtTokenService
from tests.unit.fakes import FakePasswordHasher, FakeUnitOfWork, InMemoryRefreshTokenStore

PASSWORD = "correct-horse-1"


@pytest.fixture
def users() -> dict[UUID, User]:
    return {}


@pytest.fixture
def uow_factory(users: dict[UUID, User]) -> UnitOfWorkFactory:
    plans = {"free": Plan(id=1, name="free", monthly_minutes=300, max_file_mb=500)}

    def factory() -> UnitOfWork:
        return FakeUnitOfWork(users, plans)

    return factory


@pytest.fixture
def tokens() -> JwtTokenService:
    return JwtTokenService(SecretStr("unit-test-secret-0123456789-0123456789"), "HS256", 15, 7)


@pytest.fixture
def store() -> InMemoryRefreshTokenStore:
    return InMemoryRefreshTokenStore()


@pytest.fixture
def register(uow_factory: UnitOfWorkFactory) -> RegisterUser:
    return RegisterUser(uow_factory, FakePasswordHasher())


@pytest.fixture
def login(
    uow_factory: UnitOfWorkFactory, tokens: JwtTokenService, store: InMemoryRefreshTokenStore
) -> LoginUser:
    return LoginUser(uow_factory, FakePasswordHasher(), tokens, store)


async def test_register_normalizes_email_and_hashes_password(register: RegisterUser) -> None:
    user = await register.execute("  Foo@Example.com ", PASSWORD)
    assert user.email == "foo@example.com"
    assert user.password_hash != PASSWORD
    assert user.plan.name == "free"


async def test_register_duplicate_email(register: RegisterUser) -> None:
    await register.execute("foo@example.com", PASSWORD)
    with pytest.raises(EmailTakenError):
        await register.execute("FOO@example.com", PASSWORD)


async def test_login_success_saves_refresh_token(
    register: RegisterUser, login: LoginUser, store: InMemoryRefreshTokenStore
) -> None:
    await register.execute("foo@example.com", PASSWORD)
    pair = await login.execute("Foo@Example.com", PASSWORD)
    assert pair.access_token and pair.refresh_token
    assert pair.expires_in == 900
    assert len(store.items) == 1


async def test_login_wrong_password_and_unknown_user(
    register: RegisterUser, login: LoginUser
) -> None:
    await register.execute("foo@example.com", PASSWORD)
    with pytest.raises(UnauthorizedError):
        await login.execute("foo@example.com", "wrong-password-1")
    with pytest.raises(UnauthorizedError):
        await login.execute("nobody@example.com", PASSWORD)


async def test_refresh_rotates_and_rejects_reuse(
    uow_factory: UnitOfWorkFactory,
    register: RegisterUser,
    login: LoginUser,
    tokens: JwtTokenService,
    store: InMemoryRefreshTokenStore,
) -> None:
    await register.execute("foo@example.com", PASSWORD)
    first = await login.execute("foo@example.com", PASSWORD)
    refresh = RefreshTokens(uow_factory, tokens, store)

    second = await refresh.execute(first.refresh_token)
    assert second.refresh_token != first.refresh_token
    with pytest.raises(UnauthorizedError):
        await refresh.execute(first.refresh_token)
    await refresh.execute(second.refresh_token)


async def test_logout_revokes_refresh_token(
    uow_factory: UnitOfWorkFactory,
    register: RegisterUser,
    login: LoginUser,
    tokens: JwtTokenService,
    store: InMemoryRefreshTokenStore,
) -> None:
    await register.execute("foo@example.com", PASSWORD)
    pair = await login.execute("foo@example.com", PASSWORD)

    await LogoutUser(tokens, store).execute(pair.refresh_token)
    assert store.items == {}
    with pytest.raises(UnauthorizedError):
        await RefreshTokens(uow_factory, tokens, store).execute(pair.refresh_token)
    await LogoutUser(tokens, store).execute("garbage")  # ідемпотентно, без помилки
