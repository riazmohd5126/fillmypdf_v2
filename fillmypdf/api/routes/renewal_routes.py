"""
PA Renewal API
===============
CRUD for renewal-tracking records (drug/payer/member + when a PA needs to
be renewed) plus a lightweight due-count endpoint the left-nav badge polls.
Owner-scoped like profiles: a clinic sees only its own records, admin sees
everything.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ...models.renewal import Renewal, RenewalCreate, RenewalDueCount, RenewalUpdate
from ...services.renewal_service import RenewalAccessDenied, RenewalNotFound, RenewalService
from ..dependencies.auth import require_api_key

router = APIRouter(
    prefix="/renewals",
    tags=["renewals"],
    dependencies=[Depends(require_api_key)],
)

renewal_service = RenewalService()


def _owner(api_key: dict) -> Optional[str]:
    return api_key.get("id")


def _tier(api_key: dict) -> str:
    return api_key.get("tier", "free")


@router.post("/", response_model=Renewal, status_code=201)
async def create_renewal(body: RenewalCreate, api_key: dict = Depends(require_api_key)):
    return renewal_service.create(body, owner_id=_owner(api_key))


@router.get("/", response_model=list[Renewal])
async def list_renewals(api_key: dict = Depends(require_api_key)):
    """Sorted soonest-due first (overdue, then due_soon, then upcoming...)."""
    return renewal_service.list(owner_id=_owner(api_key), tier=_tier(api_key))


@router.get("/due-count", response_model=RenewalDueCount, summary="Count for the left-nav badge")
async def renewal_due_count(api_key: dict = Depends(require_api_key)):
    return renewal_service.due_count(owner_id=_owner(api_key), tier=_tier(api_key))


@router.get("/{renewal_id}", response_model=Renewal)
async def get_renewal(renewal_id: str, api_key: dict = Depends(require_api_key)):
    try:
        return renewal_service.get(renewal_id, owner_id=_owner(api_key), tier=_tier(api_key))
    except RenewalNotFound:
        raise HTTPException(404, "Renewal record not found")
    except RenewalAccessDenied:
        raise HTTPException(403, "Not your renewal record")


@router.patch("/{renewal_id}", response_model=Renewal)
async def update_renewal(renewal_id: str, body: RenewalUpdate, api_key: dict = Depends(require_api_key)):
    try:
        return renewal_service.update(renewal_id, body, owner_id=_owner(api_key), tier=_tier(api_key))
    except RenewalNotFound:
        raise HTTPException(404, "Renewal record not found")
    except RenewalAccessDenied:
        raise HTTPException(403, "Not your renewal record")


@router.delete("/{renewal_id}", status_code=204)
async def delete_renewal(renewal_id: str, api_key: dict = Depends(require_api_key)):
    try:
        renewal_service.delete(renewal_id, owner_id=_owner(api_key), tier=_tier(api_key))
    except RenewalNotFound:
        raise HTTPException(404, "Renewal record not found")
    except RenewalAccessDenied:
        raise HTTPException(403, "Not your renewal record")
