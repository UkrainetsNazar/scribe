import asyncio
from uuid import UUID

from scribe.application.pipeline_ports import PipelineDispatcher
from scribe.domain.pipeline import PIPELINE_STAGES
from scribe.infrastructure.celery_app import PIPELINE_QUEUE, celery_app, stage_task_name


class CeleryPipelineDispatcher(PipelineDispatcher):
    async def dispatch(self, recording_id: UUID, owner_id: UUID, run: int) -> None:
        await asyncio.to_thread(self._send, recording_id, owner_id, run)  # kombu is blocking

    @staticmethod
    def _send(recording_id: UUID, owner_id: UUID, run: int) -> None:
        celery_app.signature(
            stage_task_name(PIPELINE_STAGES[0]),
            args=[str(recording_id), str(owner_id), run],
            queue=PIPELINE_QUEUE,
        ).apply_async()
