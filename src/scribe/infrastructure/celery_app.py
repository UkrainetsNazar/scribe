from celery import Celery
from kombu.entity import Queue

from scribe.config import get_settings
from scribe.domain.pipeline import StageName

PIPELINE_QUEUE = "celery.pipeline"
DLQ_QUEUE = "celery.pipeline.dlq"
DEAD_LETTER_TASK = "scribe.pipeline.dead_letter"

# acks_late keeps a message unacknowledged while a long stage runs, and RabbitMQ closes the
# channel after `consumer_timeout` (30 min by default). Transcribing 4 hours can take longer.
CONSUMER_TIMEOUT_MS = 6 * 3600 * 1000


def stage_task_name(stage: StageName) -> str:
    return f"scribe.pipeline.{stage.value}"


def create_celery_app() -> Celery:
    settings = get_settings()
    app = Celery("scribe", broker=settings.rabbitmq_url)
    app.conf.update(
        task_default_queue=PIPELINE_QUEUE,
        task_queues=(
            Queue(
                PIPELINE_QUEUE,
                queue_arguments={"x-consumer-timeout": CONSUMER_TIMEOUT_MS},
            ),  # type: ignore[no-untyped-call]
            Queue(DLQ_QUEUE),  # type: ignore[no-untyped-call]
        ),
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_ignore_result=True,  # state lives in the database, not in a result backend
        worker_prefetch_multiplier=1,
        worker_hijack_root_logger=False,
        broker_connection_retry_on_startup=True,
        task_serializer="json",
        accept_content=["json"],
        timezone="UTC",
    )
    return app


celery_app = create_celery_app()
