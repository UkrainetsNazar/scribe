import json
from uuid import uuid4

import pytest

from scribe.domain.errors import DomainValidationError, InvalidStateError
from scribe.domain.projects import Project
from scribe.domain.webhooks import (
    generate_secret,
    sign,
    signature_header,
    validate_webhook_url,
    verify_signature,
    webhook_body,
)

SECRET = "whsec_test"
BODY = b'{"a":1}'


def test_signature_roundtrip() -> None:
    header = signature_header(SECRET, 1_000, BODY)
    assert header == f"t=1000,v1={sign(SECRET, 1_000, BODY)}"
    assert verify_signature(SECRET, header, BODY, now=1_000)


def test_tampering_is_detected() -> None:
    header = signature_header(SECRET, 1_000, BODY)
    assert not verify_signature(SECRET, header, b'{"a":2}', now=1_000)
    assert not verify_signature("other", header, BODY, now=1_000)
    forged = header.replace("t=1000", "t=1001")  # the timestamp is part of the signed data
    assert not verify_signature(SECRET, forged, BODY, now=1_001)


def test_stale_signatures_are_rejected() -> None:
    header = signature_header(SECRET, 1_000, BODY)
    assert verify_signature(SECRET, header, BODY, now=1_300)
    assert not verify_signature(SECRET, header, BODY, now=1_301)
    assert not verify_signature(SECRET, header, BODY, now=699)


@pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=00", "v1=00", "t=1000"])
def test_malformed_headers_are_rejected(header: str) -> None:
    assert not verify_signature(SECRET, header, BODY, now=1_000)


def test_body_is_deterministic_and_keeps_unicode() -> None:
    event_id = uuid4()
    first = webhook_body(event_id, "recording.completed", "t", {"b": 1, "title": "Зустріч", "a": 2})
    second = webhook_body(
        event_id, "recording.completed", "t", {"a": 2, "title": "Зустріч", "b": 1}
    )
    assert first == second
    assert "Зустріч" in first.decode()  # not escaped to \uXXXX
    assert json.loads(first)["id"] == str(event_id)


@pytest.mark.parametrize(
    "url",
    ["https://example.com/hook", "http://localhost:9999/x?token=1", "  https://example.com/h "],
)
def test_valid_urls(url: str) -> None:
    assert validate_webhook_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/x",
        "example.com/hook",
        "https://",
        "https://user:pw@example.com/x",
        "https://example.com:99999/x",
        "https://" + "a" * 2050,
    ],
)
def test_invalid_urls(url: str) -> None:
    with pytest.raises(DomainValidationError):
        validate_webhook_url(url)


def test_project_webhook_lifecycle() -> None:
    project = Project.create(uuid4(), "P")
    assert (project.webhook_url, project.webhook_secret) == (None, None)

    project.set_webhook("https://example.com/hook")
    first = project.webhook_secret
    assert first is not None and first.startswith("whsec_")

    project.set_webhook("https://example.org/other")  # a new URL keeps the secret
    assert project.webhook_secret == first

    project.rotate_webhook_secret()
    assert project.webhook_secret != first

    project.clear_webhook()
    assert (project.webhook_url, project.webhook_secret) == (None, None)


def test_rotating_requires_a_url() -> None:
    with pytest.raises(InvalidStateError):
        Project.create(uuid4(), "P").rotate_webhook_secret()


def test_secrets_are_unique() -> None:
    assert generate_secret() != generate_secret()
