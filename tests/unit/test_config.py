import pytest
from pydantic import ValidationError

from scribe.config import DEV_JWT_SECRET, Settings


@pytest.fixture(autouse=True)
def _no_secret_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def test_the_public_default_secret_is_accepted_only_in_tests() -> None:
    assert _settings(app_env="test").jwt_secret.get_secret_value() == DEV_JWT_SECRET
    for env in ("local", "prod"):
        with pytest.raises(ValidationError):
            _settings(app_env=env)


@pytest.mark.parametrize("secret", ["", "short", "x" * 31])
def test_short_secrets_are_rejected(secret: str) -> None:
    with pytest.raises(ValidationError):
        _settings(app_env="local", jwt_secret=secret)


def test_a_long_private_secret_is_accepted() -> None:
    assert _settings(app_env="prod", jwt_secret="k" * 32)
