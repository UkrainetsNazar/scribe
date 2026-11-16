from collections.abc import Iterator
from uuid import UUID

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.infrastructure.db.models import OutboxEventModel
from scribe.infrastructure.messaging.publisher import RabbitMqPublisher
from scribe.infrastructure.messaging.relay import OutboxRelay
from tests.integration.conftest import RabbitBroker
from tests.integration.helpers import auth_headers
from tests.integration.test_messaging import (  # noqa: F401  (_clean_state is an autouse fixture)
    _clean_state,
    _event,
    _start_consumer,
    _store_event,
    _wait_for,
)
from tests.integration.test_recordings_api import _create_recording, _project
from tests.unit.fakes import CountingHandler

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture(scope="module")
def spans() -> Iterator[InMemorySpanExporter]:
    """The global provider can be set once per process; this module owns it."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


async def test_the_request_trace_id_is_stored_with_the_event(
    spans: InMemorySpanExporter, api_client: httpx.AsyncClient, session_factory: Sessions
) -> None:
    headers = await auth_headers(api_client)
    project_id = await _project(api_client, headers)
    resp = await _create_recording(api_client, headers, project_id, 1024)
    assert resp.status_code == 201
    recording_id = UUID(resp.json()["recording"]["id"])
    trace_id = resp.headers["x-trace-id"]

    # It is a real OpenTelemetry trace (a span with this id was recorded), not a random request id
    assert any(format(s.context.trace_id, "032x") == trace_id for s in spans.get_finished_spans())

    async with session_factory() as session:
        stmt = select(OutboxEventModel).where(
            OutboxEventModel.aggregate_id == recording_id,
            OutboxEventModel.event_type == "recording.created",
        )
        row = (await session.execute(stmt)).scalar_one()
    assert row.headers["traceparent"].split("-")[1] == trace_id
    assert row.headers["trace_id"] == trace_id


async def test_the_trace_continues_through_the_broker_to_the_consumer(
    spans: InMemorySpanExporter, rabbit: RabbitBroker, session_factory: Sessions
) -> None:
    publisher = RabbitMqPublisher(rabbit.url, (1,), timeout_sec=5)
    await publisher.connect()
    handler = CountingHandler()
    consumer = await _start_consumer(rabbit, session_factory, handler, (1,))
    try:
        with trace.get_tracer("test").start_as_current_span("request") as root:
            await _store_event(session_factory, _event())  # commit() stores the trace context
        trace_id = root.get_span_context().trace_id

        relay = OutboxRelay(session_factory, publisher, batch_size=10, poll_interval_sec=0.05)
        assert await relay.run_once() == 1

        expected = {
            "request",
            "recording.processing_requested publish",
            "recording.processing_requested process",
        }

        def names() -> set[str]:
            return {s.name for s in spans.get_finished_spans() if s.context.trace_id == trace_id}

        await _wait_for(lambda: expected <= names())
        assert handler.calls == 1
    finally:
        await consumer.close()
        await publisher.close()
