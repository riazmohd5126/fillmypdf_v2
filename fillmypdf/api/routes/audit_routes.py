"""
Activity audit API
==================
GET /api/v1/audit — recent mutating clinic/admin actions.
Clinic callers see their org (or API key); admin sees everything.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from ...models.activity_audit import ActivityAuditEvent, ActivityAuditListResponse
from ...services.activity_audit_service import ActivityAuditService
from ..dependencies.auth import require_api_key

router = APIRouter(
    prefix="/audit",
    tags=["audit"],
    dependencies=[Depends(require_api_key)],
)

_svc = ActivityAuditService()


def _is_admin(request: Request, api_key: dict) -> bool:
    user = getattr(request.state, "user", None) or {}
    return api_key.get("tier") == "admin" or str(user.get("role") or "").lower() == "admin"


@router.get(
    "",
    response_model=ActivityAuditListResponse,
    summary="List recent activity audit events",
)
async def list_activity(
    request: Request,
    api_key: dict = Depends(require_api_key),
    limit: int = Query(default=100, ge=1, le=500),
    event: Optional[str] = Query(default=None, description="Event name or prefix, e.g. template.fill or auth."),
):
    admin = _is_admin(request, api_key)
    user = getattr(request.state, "user", None) or {}
    org_id = None if admin else (user.get("org_id") or api_key.get("org_id"))
    key_id = None if admin else api_key.get("id")
    raw = _svc.list_recent(
        limit=limit,
        event=event,
        org_id=org_id,
        api_key_id=key_id if not org_id else None,
        admin=admin,
    )
    events = [ActivityAuditEvent(**row) for row in raw]
    return ActivityAuditListResponse(events=events, count=len(events))
