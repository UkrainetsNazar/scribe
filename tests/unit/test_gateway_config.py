from pathlib import Path
from typing import Any

import pytest
import yaml

CONFIG = Path(__file__).resolve().parents[2] / "deploy" / "traefik" / "dynamic.yml"


@pytest.fixture(scope="module")
def http() -> dict[str, Any]:
    return dict(yaml.safe_load(CONFIG.read_text())["http"])


def test_every_referenced_middleware_and_service_exists(http: dict[str, Any]) -> None:
    for name, router in http["routers"].items():
        assert router["service"] in http["services"], name
        for middleware in router.get("middlewares", []):
            assert middleware in http["middlewares"], f"{name}: {middleware}"


def test_middleware_chains(http: dict[str, Any]) -> None:
    routers = http["routers"]
    # CORS first (a preflight is answered before authentication), rate limit before the auth call
    assert routers["rest"]["middlewares"] == ["cors", "rate-limit", "jwt-auth"]
    assert "rate-limit-auth" in routers["auth"]["middlewares"]
    assert "jwt-auth" not in routers["auth"]["middlewares"]  # nobody has a token yet
    assert "rate-limit" in routers["ws"]["middlewares"]


def test_internal_and_metrics_endpoints_are_never_routed(http: dict[str, Any]) -> None:
    for name, router in http["routers"].items():
        assert "/internal" not in router["rule"], name
        assert "/metrics" not in router["rule"], name


def test_auth_limit_is_stricter_than_the_general_limit(http: dict[str, Any]) -> None:
    def per_second(name: str) -> float:
        limit = http["middlewares"][name]["rateLimit"]
        return float(limit["average"]) / {"1s": 1, "1m": 60}[limit["period"]]

    assert per_second("rate-limit-auth") < per_second("rate-limit")
