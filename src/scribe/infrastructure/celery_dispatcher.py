import asyncio
from typing import Any
from uuid import UUID

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind

from scribe.application.pipeline_ports import PipelineDispatcher
from scribe.domain.pipeline import PIPELINE_STAGES
from scribe.infrastructure.celery_app import PIPELINE_QUEUE, celery_app, stage_task_name

_tracer = trace.get_tracer("scribe.dispatch")


class CeleryPipelineDispatcher(PipelineDispatcher):
    async def dispatch(self, recording_id: UUID, owner_id: UUID, run: int) -> None:
        await asyncio.to_thread(self._send, recording_id, owner_id, run)  # kombu is blocking

    @staticmethod
    def _send(recording_id: UUID, owner_id: UUID, run: int) -> None:
        first = PIPELINE_STAGES[0]
        with _tracer.start_as_current_span(
            f"dispatch {stage_task_name(first)}",
            kind=SpanKind.PRODUCER,
            attributes={"scribe.recording_id": str(recording_id), "scribe.run": run},
        ):
            headers: dict[str, Any] = {}
            propagate.inject(headers)  # the worker's task span becomes a child of this span
            celery_app.signature(
                stage_task_name(first),
                args=[str(recording_id), str(owner_id), run],
                queue=PIPELINE_QUEUE,
            ).apply_async(headers=headers)
