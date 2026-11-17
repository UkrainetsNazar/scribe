from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from scribe.domain.recordings import Recording


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    url: str
    secret: str
    event_id: UUID  # stable across retries: receivers can deduplicate by it
    event_type: str
    body: bytes


class WebhookDeliveryError(Exception):
    """A failed attempt that may succeed later: the message goes through the retry queues."""


class WebhookSender(ABC):
    @abstractmethod
    async def send(self, delivery: WebhookDelivery) -> None:
        """Raises WebhookDeliveryError (retried) or PermanentHandlerError (straight to the DLQ)."""


def build_webhook_data(
    event_type: str, payload: Mapping[str, Any], recording: Recording
) -> dict[str, Any]:
    failed = event_type == "recording.failed"
    data: dict[str, Any] = {
        "recording_id": str(recording.id),
        "project_id": str(recording.project_id),
        "title": recording.title,
        "status": "failed" if failed else "completed",
        "run": payload.get("run"),
        "duration_sec": recording.duration_sec,
        "language": recording.language,
    }
    if failed:
        data["stage"] = payload.get("stage")
        data["error"] = payload.get("reason")
    return data
