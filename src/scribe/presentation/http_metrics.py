import re
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from scribe.infrastructure.metrics import HTTP_DURATION, HTTP_REQUESTS

_SKIPPED_PREFIXES = ("/metrics", "/health/", "/internal/")
_ID_SEGMENT = re.compile(r"^(/api/v1/(?:recordings|projects))/[^/]+")
_EXPORT_FORMAT = re.compile(r"/exports/[^/]+$")


def handler_label(path: str, status: int) -> str:
    """A low-cardinality label: ids and export formats are collapsed, unknown paths are grouped.

    Every 404 shares one label, so a scanner cannot create an unbounded number of series.
    """
    if status == 404:
        return "unmatched"
    path = _ID_SEGMENT.sub(r"\1/{id}", path)
    path = _EXPORT_FORMAT.sub("/exports/{format}", path)
    if path.startswith(("/api/", "/docs", "/redoc", "/openapi.json")):
        return path
    return "other"


class HttpMetricsMiddleware:
    """RED metrics for HTTP requests (WebSocket connections have their own gauge)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(_SKIPPED_PREFIXES):
            await self.app(scope, receive, send)
            return

        status = 500  # stays 500 if the app raises before answering
        started = time.perf_counter()

        async def send_and_record_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_and_record_status)
        finally:
            handler = handler_label(scope["path"], status)
            method = scope["method"]
            HTTP_REQUESTS.labels(method, handler, str(status)).inc()
            HTTP_DURATION.labels(method, handler).observe(time.perf_counter() - started)
