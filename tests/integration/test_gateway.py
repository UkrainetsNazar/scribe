import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from dependency_injector import providers
from testcontainers.core.container import DockerContainer
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from scribe.config import Settings
from scribe.container import Container
from scribe.presentation.app import create_app
from tests.integration.helpers import API, ROOT, auth_headers
from tests.integration.test_websocket import LiveServer, _free_port, _recv, _setup, _url

pytestmark = pytest.mark.integration

GATEWAY_IMAGE = "traefik:v3.3"  # keep in sync with docker-compose.yml
CONFIG_DIR = ROOT / "deploy" / "traefik"
ALLOWED_ORIGIN = "http://localhost:3000"


def _prepare_config(target: Path, upstream: str) -> None:
    """The production config, with both services pointing at the test server."""
    (target / "traefik.yml").write_text((CONFIG_DIR / "traefik.yml").read_text())
    dynamic = (CONFIG_DIR / "dynamic.yml").read_text()
    for service in ("api", "ws"):
        dynamic = dynamic.replace(f"http://{service}:8000", upstream)
    (target / "dynamic.yml").write_text(dynamic)


async def _wait_until_routing(url: str, container: DockerContainer) -> None:
    deadline = time.monotonic() + 60
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(url, timeout=3)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    out, err = container.get_logs()
    raise RuntimeError(f"The gateway did not start routing:\n{out.decode()}\n{err.decode()}")


@pytest.fixture(scope="module")
async def gateway(
    app_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[LiveServer]:
    container = Container()
    container.settings.override(providers.Object(app_settings))
    port = _free_port()
    config = uvicorn.Config(
        create_app(container),
        host="0.0.0.0",  # noqa: S104  (the gateway container reaches the host through the Docker bridge)
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        if task.done():
            task.result()  # surfaces a startup error
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("The test server did not start in time")

    config_dir = tmp_path_factory.mktemp("traefik")
    _prepare_config(config_dir, f"http://host.docker.internal:{port}")
    traefik = (
        DockerContainer(GATEWAY_IMAGE)
        .with_exposed_ports(8000)
        .with_volume_mapping(str(config_dir), "/etc/traefik", "ro")
        .with_kwargs(extra_hosts={"host.docker.internal": "host-gateway"})
    )
    await asyncio.to_thread(traefik.start)
    try:
        host, gateway_port = traefik.get_container_host_ip(), traefik.get_exposed_port(8000)
        await _wait_until_routing(f"http://{host}:{gateway_port}/health/live", traefik)
        yield LiveServer(
            http_url=f"http://{host}:{gateway_port}", ws_url=f"ws://{host}:{gateway_port}"
        )
    finally:
        await asyncio.to_thread(traefik.stop)
        server.should_exit = True
        await task


async def test_public_routes_need_no_token(gateway: LiveServer) -> None:
    async with httpx.AsyncClient(base_url=gateway.http_url) as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/openapi.json")).status_code == 200


async def test_protected_routes_are_checked_by_the_gateway(gateway: LiveServer) -> None:
    async with httpx.AsyncClient(base_url=gateway.http_url) as client:
        # /api/v1/nope does not exist, so only the gateway can answer 401 here (the app says 404)
        for headers in ({}, {"Authorization": "Bearer garbage"}):
            resp = await client.get(f"{API}/nope", headers=headers)
            assert resp.status_code == 401
            assert resp.headers["content-type"].startswith("application/problem+json")
            assert resp.headers["www-authenticate"] == "Bearer"

        headers = await auth_headers(client)  # register and login go through the gateway too
        assert (await client.get(f"{API}/users/me", headers=headers)).status_code == 200
        assert (await client.get(f"{API}/nope", headers=headers)).status_code == 404  # passed


async def test_internal_endpoints_are_not_exposed(gateway: LiveServer) -> None:
    async with httpx.AsyncClient(base_url=gateway.http_url) as client:
        resp = await client.get("/internal/auth", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 404


async def test_cors(gateway: LiveServer) -> None:
    preflight = {
        "Origin": ALLOWED_ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    async with httpx.AsyncClient(base_url=gateway.http_url) as client:
        # Answered by the gateway itself: no token is needed for a preflight
        resp = await client.options(f"{API}/projects", headers=preflight)
        assert resp.status_code in (200, 204)
        assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        assert "authorization" in resp.headers["access-control-allow-headers"].lower()

        foreign = await client.options(
            f"{API}/projects", headers={**preflight, "Origin": "http://evil.example"}
        )
        assert "access-control-allow-origin" not in foreign.headers

        resp = await client.get("/health/live", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        assert "x-trace-id" in resp.headers["access-control-expose-headers"].lower()


async def test_websocket_upgrade_works_through_the_gateway(gateway: LiveServer) -> None:
    token, recording_id = await _setup(gateway)
    async with connect(_url(gateway, recording_id, token)) as ws:
        first = await _recv(ws)
    assert (first["type"], first["status"]) == ("status", "created")


async def test_websocket_handshake_is_still_authenticated(gateway: LiveServer) -> None:
    _, recording_id = await _setup(gateway)
    url = f"{gateway.ws_url}/ws/recordings/{recording_id}?token=garbage"
    with pytest.raises(InvalidStatus) as info:
        async with connect(url):
            pass
    assert info.value.response.status_code == 403


async def test_rate_limit_returns_429_with_retry_after(gateway: LiveServer) -> None:
    """Keep this test last: it drains the bucket of the (shared) client IP."""
    limits = httpx.Limits(max_connections=100)
    async with httpx.AsyncClient(base_url=gateway.http_url, limits=limits, timeout=30) as client:
        responses = await asyncio.gather(*(client.get("/health/live") for _ in range(1000)))

    limited = [r for r in responses if r.status_code == 429]
    assert limited, "no request was rate limited"
    assert {r.status_code for r in responses} <= {200, 429}
    assert "retry-after" in limited[0].headers
