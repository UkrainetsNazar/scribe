from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return dict(yaml.safe_load(COMPOSE.read_text()))


def test_published_ports_are_bound_to_localhost(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name}: {port}"


def test_application_services_publish_no_ports(compose: dict[str, Any]) -> None:
    for name in ("api", "ws", "relay", "consumer", "worker", "migrate"):
        assert "ports" not in compose["services"][name], name


def test_compose_requires_a_jwt_secret(compose: dict[str, Any]) -> None:
    assert str(compose["x-app-env"]["JWT_SECRET"]).startswith("${JWT_SECRET:?")


def test_webhooks_cannot_reach_private_networks_by_default(compose: dict[str, Any]) -> None:
    value = str(compose["x-app-env"]["WEBHOOK_ALLOW_PRIVATE_NETWORKS"])
    assert value.endswith(":-false}")
