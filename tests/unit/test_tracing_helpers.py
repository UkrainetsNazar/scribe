import structlog
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider

from scribe.infrastructure.telemetry import current_trace_id, trace_headers


def test_no_trace_id_outside_a_span() -> None:
    assert current_trace_id() is None


def test_headers_carry_the_active_span() -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("request") as span:
        headers = trace_headers()
        expected = format(span.get_span_context().trace_id, "032x")
    assert headers["trace_id"] == expected
    assert headers["traceparent"].split("-")[1] == expected


def test_context_survives_the_outbox_roundtrip() -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("request") as span:
        headers = trace_headers()
    restored = trace.get_current_span(propagate.extract(headers)).get_span_context()
    assert restored.trace_id == span.get_span_context().trace_id
    assert restored.is_remote


def test_without_tracing_the_request_id_is_kept_for_logs() -> None:
    structlog.contextvars.bind_contextvars(trace_id="request-1")
    try:
        assert trace_headers() == {"trace_id": "request-1"}
    finally:
        structlog.contextvars.clear_contextvars()
