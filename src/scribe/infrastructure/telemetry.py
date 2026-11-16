from typing import Any

import structlog
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from scribe.config import Settings

_configured = False


def setup_tracing(settings: Settings) -> bool:
    """Installs the global tracer provider once per process. False when tracing is off.

    Call it in every process. With prefork Celery, call it in each pool process after the
    fork: exporter threads do not survive a fork.
    """
    global _configured
    if not settings.otlp_endpoint:
        return False
    if not _configured:
        provider = TracerProvider(
            resource=Resource.create(
                {"service.name": settings.service_name, "deployment.environment": settings.app_env}
            )
        )
        exporter = OTLPSpanExporter(endpoint=f"{settings.otlp_endpoint.rstrip('/')}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _configured = True
    return True


def current_trace_id() -> str | None:
    """The active OpenTelemetry trace id as 32 hex characters, or None outside a span."""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def trace_headers() -> dict[str, Any]:
    """Headers stored with an outbox event: the W3C trace context (`traceparent`) and the
    trace id for log correlation. Without tracing, the request id of the logging context."""
    headers: dict[str, Any] = {}
    trace_id = current_trace_id() or structlog.contextvars.get_contextvars().get("trace_id")
    if trace_id:
        headers["trace_id"] = trace_id
    propagate.inject(headers)
    return headers
