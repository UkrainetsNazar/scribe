import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from scribe.infrastructure.telemetry import current_trace_id


class TraceIdMiddleware:
    """Gives every request a trace_id: the OpenTelemetry trace id when tracing is on (so logs,
    error bodies and Jaeger agree), otherwise X-Request-ID or a random id.

    FastAPI's telemetry layer wraps this middleware, so the request span is already active."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        trace_id = (
            current_trace_id()
            or (Headers(scope=scope).get("x-request-id") or uuid.uuid4().hex)[:64]
        )
        scope["trace_id"] = trace_id
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

        async def send_with_trace_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Trace-Id"] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace_id)
        finally:
            structlog.contextvars.unbind_contextvars("trace_id")
