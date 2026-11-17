from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from scribe.domain.errors import AppError

log = structlog.get_logger()

PROBLEMS: dict[str, tuple[int, str]] = {
    "BAD_REQUEST": (400, "Bad request"),
    "UNAUTHORIZED": (401, "Unauthorized"),
    "TOKEN_EXPIRED": (401, "Token expired"),
    "FORBIDDEN": (403, "Forbidden"),
    "NOT_FOUND": (404, "Not found"),
    "INVALID_STATE": (409, "Invalid state"),
    "EMAIL_TAKEN": (409, "Email already taken"),
    "FILE_TOO_LARGE": (413, "File too large"),
    "UNSUPPORTED_MEDIA": (415, "Unsupported media"),
    "VALIDATION_ERROR": (422, "Validation error"),
    "QUOTA_EXCEEDED": (422, "Quota exceeded"),
    "RATE_LIMITED": (429, "Too many requests"),
    "INTERNAL_ERROR": (500, "Internal server error"),
    "DEPENDENCY_UNAVAILABLE": (503, "Dependency unavailable"),
    "PROJECT_NAME_TAKEN": (409, "Project name already taken"),
    "INVALID_MEDIA": (422, "Invalid media"),
    "UPLOAD_MISSING": (422, "Upload missing"),
}

_HTTP_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    429: "RATE_LIMITED",
}


def _problem(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://scribe.dev/errors/{code.lower().replace('_', '-')}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        "trace_id": request.scope.get("trace_id"),
        "errors": errors or [],
    }
    response_headers = dict(headers or {})
    if status == 401:
        response_headers.setdefault("WWW-Authenticate", "Bearer")
    return JSONResponse(
        body,
        status_code=status,
        media_type="application/problem+json",
        headers=response_headers,
    )


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    status, title = PROBLEMS.get(exc.code, PROBLEMS["INTERNAL_ERROR"])
    return _problem(request, status=status, code=exc.code, title=title, detail=exc.message)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    if any(err.get("type") == "json_invalid" for err in exc.errors()):
        http_status, title = PROBLEMS["BAD_REQUEST"]
        return _problem(
            request,
            status=http_status,
            code="BAD_REQUEST",
            title=title,
            detail="Request body is not valid JSON",
        )
    errors = [
        {"loc": [str(part) for part in err["loc"]], "msg": err["msg"]} for err in exc.errors()
    ]
    http_status, title = PROBLEMS["VALIDATION_ERROR"]
    return _problem(
        request,
        status=http_status,
        code="VALIDATION_ERROR",
        title=title,
        detail="Request validation failed",
        errors=errors,
    )


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_STATUS_CODES.get(exc.status_code, "HTTP_ERROR")
    title = PROBLEMS.get(code, (exc.status_code, "HTTP error"))[1]
    return _problem(
        request,
        status=exc.status_code,
        code=code,
        title=title,
        detail=str(exc.detail),
        headers=dict(exc.headers) if exc.headers else None,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", path=request.url.path)
    status, title = PROBLEMS["INTERNAL_ERROR"]
    return _problem(request, status=status, code="INTERNAL_ERROR", title=title)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
