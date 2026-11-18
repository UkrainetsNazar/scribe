from typing import Any

from fastapi import FastAPI

PROBLEM_REF = {"$ref": "#/components/schemas/Problem"}
PROBLEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["type", "title", "status", "code"],
    "properties": {
        "type": {"type": "string", "format": "uri"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "code": {"type": "string", "example": "QUOTA_EXCEEDED"},
        "detail": {"type": ["string", "null"]},
        "trace_id": {"type": ["string", "null"]},
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "loc": {"type": "array", "items": {"type": "string"}},
                    "msg": {"type": "string"},
                },
            },
        },
    },
}
DESCRIPTIONS = {
    400: "Bad request",
    401: "Unauthorized",
    404: "Not found",
    409: "Conflict",
    413: "File too large",
    415: "Unsupported media type",
    422: "Validation error",
    429: "Too many requests",
    503: "Dependency unavailable",
}
AUTH_PUBLIC = frozenset(
    {
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
    }
)
_REC = "/api/v1/recordings/{recording_id}"
_PROJECT = "/api/v1/projects/{project_id}"
# Error statuses of the spec (sections 1.8 and 6) that depend on the operation itself
SPECIFIC: dict[tuple[str, str], set[int]] = {
    ("post", "/api/v1/auth/register"): {409},
    ("post", "/api/v1/auth/login"): {401},
    ("post", "/api/v1/auth/refresh"): {401},
    ("post", "/api/v1/projects"): {409},
    ("get", _PROJECT): {404},
    ("patch", _PROJECT): {404, 409},
    ("delete", _PROJECT): {404},
    ("get", f"{_PROJECT}/webhook"): {404},
    ("put", f"{_PROJECT}/webhook"): {404},
    ("delete", f"{_PROJECT}/webhook"): {404},
    ("post", f"{_PROJECT}/webhook/rotate-secret"): {404, 409},
    ("post", "/api/v1/recordings"): {404, 413, 415},
    ("get", _REC): {404},
    ("delete", _REC): {404, 409},
    ("post", f"{_REC}/complete-upload"): {404, 409},
    ("post", f"{_REC}/retry"): {404, 409},
    ("post", f"{_REC}/cancel"): {404, 409},
    ("get", f"{_REC}/transcript"): {404, 409},
    ("get", f"{_REC}/summary"): {404, 409},
    ("post", f"{_REC}/exports"): {404, 409},
    ("get", f"{_REC}/exports/{{export_format}}"): {404},
    ("get", "/api/v1/search"): {404},
    ("get", "/health/ready"): {503},
}
_METHODS = {"get", "post", "put", "patch", "delete"}


def problem_codes(method: str, path: str, operation: dict[str, Any]) -> set[int]:
    codes = set(SPECIFIC.get((method, path), ()))
    if path.startswith("/api/v1") and path not in AUTH_PUBLIC:
        codes |= {401, 429}
    if operation.get("parameters") or operation.get("requestBody"):
        codes.add(422)
    if operation.get("requestBody"):
        codes.add(400)  # malformed JSON
    if any(p.get("name") == "cursor" for p in operation.get("parameters", [])):
        codes.add(400)  # invalid cursor
    return codes


def _patch(schema: dict[str, Any]) -> None:
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    schemas["Problem"] = PROBLEM_SCHEMA
    for path, item in schema.get("paths", {}).items():
        for method, operation in item.items():
            if method not in _METHODS:
                continue
            responses = operation.setdefault("responses", {})
            for code in sorted(problem_codes(method, path, operation)):
                responses[str(code)] = {
                    "description": DESCRIPTIONS[code],
                    "content": {"application/problem+json": {"schema": PROBLEM_REF}},
                }
    if "HTTPValidationError" not in str(schema.get("paths")):
        schemas.pop("HTTPValidationError", None)
        schemas.pop("ValidationError", None)


def install_problem_responses(app: FastAPI) -> None:
    """Our errors are RFC 7807 problem+json: say so in the OpenAPI document."""
    default_openapi = app.openapi
    patched = False

    def openapi() -> dict[str, Any]:
        nonlocal patched
        schema = default_openapi()
        if not patched:
            _patch(schema)
            patched = True
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
