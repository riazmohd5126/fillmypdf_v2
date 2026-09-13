"""
PA Renewal models
==================
Tracks when a prior authorization was submitted, for which drug/payer/
member, and when it needs to be renewed — so the clinic gets an alert
before coverage lapses instead of finding out at the pharmacy counter.

v1 is alert-only: renewal_due is entered manually (from the payer's actual
determination letter) and status is set by hand when a renewal is filed.
Auto-drafting the renewal PA itself is a separate, later feature.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

RenewalStatus = Literal["active", "renewed", "cancelled"]
RenewalUrgency = Literal["overdue", "due_soon", "upcoming", "later", "no_date"]

# A record with no renewal_due (or status != active) never alerts.
_DUE_SOON_DAYS = 30
_UPCOMING_DAYS = 90


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def compute_urgency(renewal_due: Optional[str], status: RenewalStatus) -> RenewalUrgency:
    """Deterministic, not AI — just date arithmetic against today (UTC)."""
    if status != "active":
        return "no_date"
    due = _parse_date(renewal_due)
    if due is None:
        return "no_date"
    days_left = (due - datetime.now(timezone.utc).date()).days
    if days_left < 0:
        return "overdue"
    if days_left <= _DUE_SOON_DAYS:
        return "due_soon"
    if days_left <= _UPCOMING_DAYS:
        return "upcoming"
    return "later"


class RenewalCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "patient_name": "Jordan Demo",
                "member_id": "CT8840321",
                "drug_name": "Xolair (omalizumab)",
                "payer_name": "CT Medicaid",
                "submitted_at": "2026-03-01",
                "renewal_due": "2026-09-01",
                "notes": "Approved for 6 months per determination letter.",
            }
        }
    )

    patient_name: str = Field(..., min_length=1, max_length=200)
    member_id: Optional[str] = Field(default=None, max_length=100)
    drug_name: str = Field(..., min_length=1, max_length=200)
    payer_name: str = Field(..., min_length=1, max_length=200)
    submitted_at: Optional[str] = Field(default=None, description="YYYY-MM-DD — when the original PA was submitted")
    renewal_due: Optional[str] = Field(default=None, description="YYYY-MM-DD — when it must be renewed by")
    template_id: Optional[str] = Field(default=None, max_length=200, description="Template used for the original PA, if any")
    notes: str = Field(default="", max_length=2000)

    @field_validator("submitted_at", "renewal_due")
    @classmethod
    def _validate_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if _parse_date(v) is None:
            raise ValueError("Date must be in YYYY-MM-DD format")
        return v


class RenewalUpdate(BaseModel):
    patient_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    member_id: Optional[str] = Field(default=None, max_length=100)
    drug_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    payer_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    submitted_at: Optional[str] = None
    renewal_due: Optional[str] = None
    template_id: Optional[str] = Field(default=None, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[RenewalStatus] = None

    @field_validator("submitted_at", "renewal_due")
    @classmethod
    def _validate_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if _parse_date(v) is None:
            raise ValueError("Date must be in YYYY-MM-DD format")
        return v


class Renewal(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: Optional[str] = None
    patient_name: str
    member_id: Optional[str] = None
    drug_name: str
    payer_name: str
    submitted_at: Optional[str] = None
    renewal_due: Optional[str] = None
    template_id: Optional[str] = None
    notes: str = ""
    status: RenewalStatus = "active"
    created_at: str
    updated_at: str
    urgency: RenewalUrgency = "no_date"


class RenewalDueCount(BaseModel):
    overdue: int = 0
    due_soon: int = 0
    total_alerting: int = 0
