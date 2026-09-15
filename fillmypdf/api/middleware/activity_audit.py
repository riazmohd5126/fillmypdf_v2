"""
Record mutating /api/v1 calls after they complete.
Never reads the body — only path, actor from request.state, status, IP.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from ...services.activity_audit_service import ActivityAuditService


class ActivityAuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, service: ActivityAuditService | None = None):
        super().__init__(app)
        self._svc = service or ActivityAuditService()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        try:
            api_key = getattr(request.state, "api_key", None)
            user = getattr(request.state, "user", None)
            rid = getattr(request.state, "request_id", None)
            host = request.client.host if request.client else None
            self._svc.record_request(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                headers=dict(request.headers),
                client_host=host,
                request_id=rid,
                api_key=api_key if isinstance(api_key, dict) else None,
                user=user if isinstance(user, dict) else None,
            )
        except Exception:
            pass
        return response
