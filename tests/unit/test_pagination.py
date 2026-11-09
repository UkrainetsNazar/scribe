from datetime import UTC, datetime
from uuid import uuid4

import pytest

from scribe.application.pagination import Cursor, build_page, decode_cursor, encode_cursor
from scribe.domain.errors import BadRequestError


def test_cursor_roundtrip() -> None:
    cursor = Cursor(created_at=datetime.now(UTC), id=uuid4())
    assert decode_cursor(encode_cursor(cursor)) == cursor


@pytest.mark.parametrize("value", ["", "not-base64!!", "e30=", "W10="])
def test_invalid_cursor(value: str) -> None:
    with pytest.raises(BadRequestError):
        decode_cursor(value)


def test_build_page() -> None:
    def cursor_of(n: int) -> Cursor:
        return Cursor(created_at=datetime.fromtimestamp(n, UTC), id=uuid4())

    page = build_page([1, 2, 3], limit=2, cursor_of=cursor_of)
    assert page.items == [1, 2]
    assert page.next_cursor is not None

    last = build_page([1, 2], limit=2, cursor_of=cursor_of)
    assert last.items == [1, 2]
    assert last.next_cursor is None
