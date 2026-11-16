import asyncio
import os
import random
import time
from typing import Any
from uuid import UUID

import structlog
from celery.exceptions import Ignore
from celery.signals import worker_process_init, worker_ready
from opentelemetry import trace
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from prometheus_client import CollectorRegistry, multiprocess, start_http_server

from scribe.application.pipeline import StageOutcome
from scribe.config import get_settings
from scribe.domain.pipeline import (
    PIPELINE_STAGES,
    PermanentStageError,
    StageName,
    next_stage,
)
from scribe.infrastructure.celery_app import (
    DEAD_LETTER_TASK,
    DLQ_QUEUE,
    PIPELINE_QUEUE,
    celery_app,
    stage_task_name,
)
from scribe.infrastructure.metrics import STAGE_DURATION, STAGE_RUNS
from scribe.infrastructure.telemetry import setup_tracing
from scribe.workers.runtime import worker_container

log = structlog.get_logger()

MAX_RETRIES = get_settings().pipeline_max_retries


@worker_process_init.connect(weak=False)  # type: ignore[untyped-decorator]
def _init_pool_process(**_: Any) -> None:
    """Runs in every forked pool process: exporter threads do not survive a fork."""
    if setup_tracing(get_settings()):
        CeleryInstrumentor().instrument()  # type: ignore[no-untyped-call]


@worker_ready.connect(weak=False)  # type: ignore[untyped-decorator]
def _start_metrics_server(**_: Any) -> None:
    """Tasks run in pool processes, so the metrics of all of them are merged from files."""
    port = get_settings().metrics_port
    if port is None or "PROMETHEUS_MULTIPROC_DIR" not in os.environ:
        return
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    start_http_server(port, registry=registry)


def backoff_seconds(retries: int) -> float:
    """Exponential backoff with jitter: ~5s, 10s, 20s, 40s, 80s (capped at 5 minutes)."""
    return float(min(5 * 2**retries, 300) + random.uniform(0, 5))  # noqa: S311


async def _execute(stage: StageName, recording_id: UUID, owner_id: UUID, run: int) -> StageOutcome:
    async with worker_container() as container:
        return await container.stage_runner().execute(recording_id, owner_id, run, stage)


async def _mark_failed(
    stage: StageName, recording_id: UUID, owner_id: UUID, run: int, error: str
) -> None:
    async with worker_container() as container:
        await container.stage_runner().mark_failed(recording_id, owner_id, run, stage, error)


def _dispatch_next(stage: StageName, recording_id: UUID, owner_id: UUID, run: int) -> None:
    upcoming = next_stage(stage)
    if upcoming is None:
        return
    celery_app.signature(
        stage_task_name(upcoming),
        args=[str(recording_id), str(owner_id), run],
        queue=PIPELINE_QUEUE,
    ).apply_async()  # the Celery instrumentation passes the trace context on


def _dead_letter(stage: StageName, recording_id: UUID, error: str) -> None:
    try:
        celery_app.send_task(
            DEAD_LETTER_TASK, args=[str(recording_id), stage.value, error[:500]], queue=DLQ_QUEUE
        )
    except Exception:
        log.exception("pipeline.dead_letter_failed", recording_id=str(recording_id))


def _record(stage: StageName, outcome: str, started: float) -> None:
    STAGE_RUNS.labels(stage.value, outcome).inc()
    STAGE_DURATION.labels(stage.value, outcome).observe(time.perf_counter() - started)


def _run_stage_task(
    task: Any, stage: StageName, recording_id: UUID, owner_id: UUID, run: int
) -> str:
    started = time.perf_counter()
    trace.get_current_span().set_attributes(
        {"scribe.recording_id": str(recording_id), "scribe.stage": stage.value, "scribe.run": run}
    )
    bound = log.bind(recording_id=str(recording_id), stage=stage.value, run=run)
    try:
        outcome = asyncio.run(_execute(stage, recording_id, owner_id, run))
        if outcome is not StageOutcome.SKIPPED:
            # Also done for ALREADY_DONE: a crash after the commit but before this line
            # would otherwise stall the pipeline
            _dispatch_next(stage, recording_id, owner_id, run)
    except PermanentStageError as exc:
        bound.error("pipeline.permanent_error", error=str(exc))
        asyncio.run(_mark_failed(stage, recording_id, owner_id, run, str(exc)))
        _record(stage, "failed", started)
        return "failed"
    except Exception as exc:
        retries = int(task.request.retries)
        if retries >= task.max_retries:
            bound.exception("pipeline.retries_exhausted")
            error = f"retries exhausted: {exc!r}"
            asyncio.run(_mark_failed(stage, recording_id, owner_id, run, error))
            _dead_letter(stage, recording_id, error)
            _record(stage, "failed", started)
            return "failed"
        countdown = backoff_seconds(retries)
        bound.warning("pipeline.transient_error", error=repr(exc), retry_in_sec=round(countdown))
        _record(stage, "retry", started)
        raise task.retry(exc=exc, countdown=countdown) from exc

    _record(stage, outcome.value, started)
    if outcome is StageOutcome.SKIPPED:
        bound.info("pipeline.stage_skipped")
        raise Ignore  # no result, nothing else to do
    bound.info("pipeline.stage_done", outcome=outcome.value)
    return outcome.value


def _register(stage: StageName) -> None:
    def run_stage(self: Any, recording_id: str, owner_id: str, run: int) -> str:
        return _run_stage_task(self, stage, UUID(recording_id), UUID(owner_id), run)

    celery_app.task(
        bind=True,
        name=stage_task_name(stage),
        max_retries=MAX_RETRIES,
        acks_late=True,
        reject_on_worker_lost=True,
    )(run_stage)


for _stage in PIPELINE_STAGES:
    _register(_stage)
