import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from scribe.application.inbox import PermanentHandlerError
from scribe.application.webhooks import WebhookDelivery, WebhookDeliveryError, WebhookSender
from scribe.domain.webhooks import SIGNATURE_HEADER, signature_header

USER_AGENT = "Scribe-Webhooks/1.0"

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:10.0.0.1 is a private IPv4 address in disguise
    return ip.is_global and not ip.is_multicast


class HttpxWebhookSender(WebhookSender):
    """POSTs the signed body. Redirects are never followed (they could point inside the network)
    and the response body is never read.

    The destination is resolved and checked right before the request. A DNS rebinding attack
    between that check and the connection is not prevented: pin the address in an egress
    proxy if webhooks must be hardened further."""

    def __init__(
        self,
        timeout_sec: float = 10.0,
        allow_private_networks: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = resolve_host,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._timeout = timeout_sec
        self._allow_private = allow_private_networks
        self._transport = transport
        self._resolver = resolver
        self._clock = clock

    async def send(self, delivery: WebhookDelivery) -> None:
        await self._ensure_destination_is_allowed(delivery.url)
        timestamp = int(self._clock())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-Scribe-Event": delivery.event_type,
            "X-Scribe-Delivery": str(delivery.event_id),
            SIGNATURE_HEADER: signature_header(delivery.secret, timestamp, delivery.body),
        }
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._timeout, follow_redirects=False, transport=self._transport
                ) as client,
                client.stream(
                    "POST", delivery.url, content=delivery.body, headers=headers
                ) as response,
            ):
                status = response.status_code
        except httpx.HTTPError as exc:
            raise WebhookDeliveryError(f"request failed: {exc!r}") from exc
        if not 200 <= status < 300:
            raise WebhookDeliveryError(f"endpoint answered HTTP {status}")

    async def _ensure_destination_is_allowed(self, url: str) -> None:
        if self._allow_private:
            return
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            raise PermanentHandlerError("Webhook URL has no host")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            addresses = await self._resolver(host, port)
        except OSError as exc:  # DNS may recover: retry
            raise WebhookDeliveryError(f"cannot resolve {host}: {exc!r}") from exc
        if not addresses or not all(is_public_address(a) for a in addresses):
            raise PermanentHandlerError(f"Webhook destination {host} is not a public address")
