"""Clinic activity audit log models. No PHI — ids and action names only."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ActivityAuditEvent(BaseModel):
    audit_id: str
    event: str
    at: str
    method: str = ""
    path: str = ""
    status: int = 0
    actor_id: Optional[str] = None
    actor_email: Optional[str] = None
    org_id: Optional[str] = None
    api_key_id: Optional[str] = None
    client_ip: Optional[str] = None
    request_id: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None


class ActivityAuditListResponse(BaseModel):
    events: List[ActivityAuditEvent] = Field(default_factory=list)
    count: int = 0
