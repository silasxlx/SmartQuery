"""统一的 HTTP 错误信封与可安全展示的应用异常。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s,;]+")
_SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|authorization|token|secret|password|private[_-]?key|prompt)"
)


def safe_public_message(value: str) -> str:
    """清理旧路由 detail 中可能包含的路径或凭证片段。"""
    value = _ABSOLUTE_PATH.sub("[PATH]", value)
    return re.sub(
        r"(?i)(api[_-]?key|authorization|token|secret|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        value,
    )


def safe_public_details(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else safe_public_details(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [safe_public_details(item) for item in value]
    if isinstance(value, str):
        return safe_public_message(value)
    return value


@dataclass
class AppError(Exception):
    """业务错误；message 可以展示给用户，details 不应包含敏感值。"""

    code: str
    message: str
    status_code: int = 400
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)


def error_payload(
    *,
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": safe_public_message(message),
        "details": safe_public_details(details or {}),
        "request_id": request_id,
    }


def request_id_from(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    request.state.error_code = exc.code
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(
            code=exc.code,
            message=exc.message,
            details=exc.details,
            request_id=request_id_from(request),
        ),
    )


async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    # Legacy routes still raise HTTPException.  Convert their public detail to
    # the same envelope while never exposing exception tracebacks.
    detail = exc.detail
    message = safe_public_message(detail) if isinstance(detail, str) else "请求失败"
    code = "HTTP_ERROR"
    if exc.status_code == 404:
        code = "NOT_FOUND"
    elif exc.status_code == 405:
        code = "METHOD_NOT_ALLOWED"
    request.state.error_code = code
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(
            code=code,
            message=message,
            request_id=request_id_from(request),
        ),
        headers=exc.headers,
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Field names are useful for callers; validation values are deliberately
    # omitted because they may contain secrets or uploaded content.
    fields = [".".join(str(p) for p in error.get("loc", ())) for error in exc.errors()]
    request.state.error_code = "VALIDATION_ERROR"
    return JSONResponse(
        status_code=422,
        content=error_payload(
            code="VALIDATION_ERROR",
            message="请求参数校验失败",
            details={"fields": fields},
            request_id=request_id_from(request),
        ),
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Do not echo ``str(exc)``: pandas, file paths and provider exceptions can
    # contain user data or credentials.  The JSON logger records only a safe
    # error class and request correlation id.
    request.state.error_code = "INTERNAL_ERROR"
    return JSONResponse(
        status_code=500,
        content=error_payload(
            code="INTERNAL_ERROR",
            message="服务内部错误",
            request_id=request_id_from(request),
        ),
    )
