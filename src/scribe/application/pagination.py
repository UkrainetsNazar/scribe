import base64
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from scribe.domain.errors import BadRequestError


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None


def encode_cursor(cursor: Cursor) -> str:
    raw = json.dumps({"t": cursor.created_at.isoformat(), "i": str(cursor.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(value: str) -> Cursor:
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode()))
        created_at = datetime.fromisoformat(data["t"])
        if created_at.tzinfo is None:
            raise ValueError("naive datetime")
        return Cursor(created_at=created_at, id=UUID(data["i"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise BadRequestError("Invalid cursor") from exc


def build_page[T](rows: Sequence[T], limit: int, cursor_of: Callable[[T], Cursor]) -> Page[T]:
    if len(rows) > limit:
        items = list(rows[:limit])
        return Page(items=items, next_cursor=encode_cursor(cursor_of(items[-1])))
    return Page(items=list(rows), next_cursor=None)
