"""
Attach a stable correlation ID to every request and reflect it on the response.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from ..request_context import ensure_request_id


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Ensure X-Request-ID on response; populate request.state.request_id early."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = ensure_request_id(request)
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", rid)
        return response
