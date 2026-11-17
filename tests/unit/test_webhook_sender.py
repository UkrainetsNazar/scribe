import socket
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest

from scribe.application.inbox import PermanentHandlerError
from scribe.application.webhooks import WebhookDelivery, WebhookDeliveryError
from scribe.domain.webhooks import verify_signature
from scribe.infrastructure.webhook_sender import HttpxWebhookSender, is_public_address

PUBLIC = ["93.184.216.34"]
NOW = 1_700_000_000


def _resolver(*addresses: str) -> Callable[[str, int], object]:
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


def _delivery() -> WebhookDelivery:
    return WebhookDelivery(
        url="https://hooks.example.com/scribe",
        secret="whsec_x",
        event_id=uuid4(),
        event_type="recording.completed",
        body=b'{"hello":"world"}',
    )


def _sender(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    allow_private: bool = False,
    addresses: list[str] | None = None,
) -> HttpxWebhookSender:
    return HttpxWebhookSender(
        timeout_sec=5,
        allow_private_networks=allow_private,
        transport=httpx.MockTransport(handler),
        resolver=_resolver(*(addresses or PUBLIC)),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


async def test_request_is_signed_and_describes_the_delivery() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    delivery = _delivery()
    await _sender(handler).send(delivery)

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == delivery.url
    assert request.content == delivery.body
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-scribe-event"] == "recording.completed"
    assert request.headers["x-scribe-delivery"] == str(delivery.event_id)
    assert verify_signature(
        "whsec_x", request.headers["x-scribe-signature"], request.content, now=NOW
    )


@pytest.mark.parametrize("status", [301, 302, 400, 404, 429, 500, 503])
async def test_unsuccessful_answers_are_retryable_failures(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"location": "https://elsewhere.example"})

    with pytest.raises(WebhookDeliveryError):  # redirects are not followed
        await _sender(handler).send(_delivery())


async def test_network_errors_are_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(WebhookDeliveryError):
        await _sender(handler).send(_delivery())


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.10",
        "172.16.0.1",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:10.0.0.1",
    ],
)
async def test_private_destinations_are_rejected_permanently(address: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    with pytest.raises(PermanentHandlerError):
        await _sender(handler, addresses=[address]).send(_delivery())
    assert calls == []  # nothing was sent


async def test_one_private_address_among_public_ones_is_enough_to_refuse() -> None:
    with pytest.raises(PermanentHandlerError):
        await _sender(lambda r: httpx.Response(200), addresses=[*PUBLIC, "10.0.0.5"]).send(
            _delivery()
        )


async def test_private_destinations_are_allowed_when_configured() -> None:
    await _sender(lambda r: httpx.Response(200), allow_private=True, addresses=["127.0.0.1"]).send(
        _delivery()
    )


async def test_unresolvable_host_is_retryable() -> None:
    async def resolve(host: str, port: int) -> list[str]:
        raise socket.gaierror("no such host")

    sender = HttpxWebhookSender(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)), resolver=resolve
    )
    with pytest.raises(WebhookDeliveryError):
        await sender.send(_delivery())


def test_is_public_address() -> None:
    assert is_public_address("93.184.216.34")
    assert is_public_address("2606:2800:220:1:248:1893:25c8:1946")
    assert not is_public_address("fe80::1%eth0")
