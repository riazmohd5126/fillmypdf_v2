"""
Central error responses with request correlation IDs.

All JSON error payloads include `"request_id"` so clients can cite it when
opening support tickets. Sensitive exception details stay out of prod responses.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Dict

from fastapi import Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from ..config import settings
from .request_context import ensure_request_id

log = logging.getLogger("fillmypdf")


def _headers_with_request_id(request: Request, rid: str) -> Dict[str, str]:
    return {"X-Request-ID": rid}


def register_exception_handlers(app) -> None:
    """Attach handlers to the FastAPI app (call once at startup)."""

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        rid = getattr(request.state, "request_id", None) or ensure_request_id(request)
        body: Dict[str, Any] = {"detail": exc.detail, "request_id": rid}
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers=_headers_with_request_id(request, rid),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = getattr(request.state, "request_id", None) or ensure_request_id(request)
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors(), "request_id": rid},
            headers=_headers_with_request_id(request, rid),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", None) or ensure_request_id(request)
        log.error(
            "Unhandled exception request_id=%s path=%s",
            rid,
            request.url.path,
            exc_info=True,
        )
        if settings.DEBUG:
            log.debug("Traceback for request_id=%s:\n%s", rid, traceback.format_exc())

        payload: Dict[str, Any] = {
            "detail": "Internal server error",
            "error": "internal_error",
            "request_id": rid,
        }
        if settings.DEBUG:
            payload["debug"] = {"type": type(exc).__name__, "message": str(exc)}

        return JSONResponse(
            status_code=500,
            content=payload,
            headers=_headers_with_request_id(request, rid),
        )


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Drop-in replacement for slowapi default (adds correlation ID + Retry-After)."""
    rid = getattr(request.state, "request_id", None) or ensure_request_id(request)
    response = JSONResponse(
        status_code=429,
        content={
            "detail": exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            "error": "rate_limit_exceeded",
            "request_id": rid,
        },
        headers=_headers_with_request_id(request, rid),
    )
    # Preserve slowapi's X-RateLimit-* headers where available
    if hasattr(exc, "retry_after"):
        retry = getattr(exc, "retry_after")
        if retry is not None:
            response.headers["Retry-After"] = str(int(retry))
    try:
        response = request.app.state.limiter._inject_headers(response, getattr(request.state, "view_rate_limit", None))
    except Exception:
        pass
    response.headers.setdefault("X-Request-ID", rid)
    return response
