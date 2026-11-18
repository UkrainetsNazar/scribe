import json
from typing import Any

import pytest
from dependency_injector import providers

from scribe.config import Settings
from scribe.container import Container
from scribe.presentation.app import create_app
from scribe.presentation.openapi import AUTH_PUBLIC

REC = "/api/v1/recordings/{recording_id}"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    container = Container()
    container.settings.override(providers.Object(Settings(app_role="all")))
    return dict(create_app(container).openapi())


def test_problem_schema_follows_the_spec(schema: dict[str, Any]) -> None:
    problem = schema["components"]["schemas"]["Problem"]
    assert set(problem["required"]) == {"type", "title", "status", "code"}
    assert {"detail", "trace_id", "errors"} <= set(problem["properties"])


@pytest.mark.parametrize(
    ("method", "path", "codes"),
    [
        ("post", "/api/v1/auth/register", {201, 409, 422}),
        ("post", "/api/v1/auth/login", {200, 401, 422}),
        ("post", "/api/v1/projects", {201, 401, 409, 422, 429}),
        ("post", "/api/v1/recordings", {201, 404, 413, 415, 422}),
        ("delete", REC, {204, 404, 409}),
        ("post", f"{REC}/complete-upload", {202, 404, 409, 422}),
        ("post", f"{REC}/retry", {202, 404, 409}),
        ("post", f"{REC}/cancel", {202, 404, 409}),
        ("get", f"{REC}/transcript", {200, 400, 404, 409}),
        ("get", f"{REC}/exports/{{export_format}}", {200, 404}),
        ("get", "/api/v1/search", {200, 404, 422}),
        ("get", "/health/ready", {200, 503}),
    ],
)
def test_documented_status_codes(
    schema: dict[str, Any], method: str, path: str, codes: set[int]
) -> None:
    assert {str(c) for c in codes} <= set(schema["paths"][path][method]["responses"])


def test_every_error_is_problem_json(schema: dict[str, Any]) -> None:
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            for code, response in operation["responses"].items():
                if int(code) >= 400:
                    assert "application/problem+json" in response["content"], (method, path, code)


def test_protected_operations_document_401_and_429(schema: dict[str, Any]) -> None:
    for path, item in schema["paths"].items():
        if path.startswith("/api/v1") and path not in AUTH_PUBLIC:
            for method, operation in item.items():
                assert {"401", "429"} <= set(operation["responses"]), (method, path)


def test_validation_errors_use_the_problem_schema(schema: dict[str, Any]) -> None:
    assert "HTTPValidationError" not in json.dumps(schema)


def test_internal_endpoints_are_not_documented(schema: dict[str, Any]) -> None:
    assert "/metrics" not in schema["paths"] and "/internal/auth" not in schema["paths"]
