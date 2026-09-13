"""
Renewal Service
================
Business logic for PA renewal tracking. Owner-scoped (like profiles) —
admin keys see everything, everyone else sees only their own clinic's
records. Urgency (overdue / due_soon / upcoming / later) is computed here
from renewal_due vs today, never stored — so it's always current on read.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from ..models.renewal import (
    Renewal,
    RenewalCreate,
    RenewalDueCount,
    RenewalUpdate,
    compute_urgency,
)
from ..repositories.renewal_repository import RenewalRepository


class RenewalNotFound(Exception):
    pass


class RenewalAccessDenied(Exception):
    pass


class RenewalService:
    def __init__(self) -> None:
        self.repo = RenewalRepository()

    # ------------------------------------------------------------------
    def _is_admin(self, tier: str) -> bool:
        return (tier or "").lower() == "admin"

    def _to_model(self, record: dict) -> Renewal:
        status = record.get("status", "active")
        return Renewal(
            id=record["id"],
            owner_id=record.get("owner_id"),
            patient_name=record.get("patient_name", ""),
            member_id=record.get("member_id"),
            drug_name=record.get("drug_name", ""),
            payer_name=record.get("payer_name", ""),
            submitted_at=record.get("submitted_at"),
            renewal_due=record.get("renewal_due"),
            template_id=record.get("template_id"),
            notes=record.get("notes", ""),
            status=status,
            created_at=record.get("created_at", ""),
            updated_at=record.get("updated_at", ""),
            urgency=compute_urgency(record.get("renewal_due"), status),
        )

    # ------------------------------------------------------------------
    def create(self, data: RenewalCreate, *, owner_id: str) -> Renewal:
        now = self.repo.now_iso()
        record = {
            "id": f"ren_{uuid.uuid4().hex[:12]}",
            "owner_id": owner_id,
            **data.model_dump(),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        self.repo.save(record)
        return self._to_model(record)

    def get(self, renewal_id: str, *, owner_id: Optional[str], tier: str = "free") -> Renewal:
        record = self.repo.get(renewal_id)
        if not record:
            raise RenewalNotFound(renewal_id)
        if not self._is_admin(tier) and record.get("owner_id") != owner_id:
            raise RenewalAccessDenied(renewal_id)
        return self._to_model(record)

    def list(self, *, owner_id: Optional[str], tier: str = "free") -> List[Renewal]:
        records = self.repo.list_all() if self._is_admin(tier) else self.repo.list_for_owner(owner_id)
        models = [self._to_model(r) for r in records]
        # Soonest due first; records with no date sort last.
        urgency_rank = {"overdue": 0, "due_soon": 1, "upcoming": 2, "later": 3, "no_date": 4}
        return sorted(
            models,
            key=lambda m: (urgency_rank.get(m.urgency, 5), m.renewal_due or "9999-99-99"),
        )

    def update(
        self, renewal_id: str, data: RenewalUpdate, *, owner_id: Optional[str], tier: str = "free"
    ) -> Renewal:
        record = self.repo.get(renewal_id)
        if not record:
            raise RenewalNotFound(renewal_id)
        if not self._is_admin(tier) and record.get("owner_id") != owner_id:
            raise RenewalAccessDenied(renewal_id)
        updates = data.model_dump(exclude_unset=True)
        record.update(updates)
        record["updated_at"] = self.repo.now_iso()
        self.repo.save(record)
        return self._to_model(record)

    def delete(self, renewal_id: str, *, owner_id: Optional[str], tier: str = "free") -> None:
        record = self.repo.get(renewal_id)
        if not record:
            raise RenewalNotFound(renewal_id)
        if not self._is_admin(tier) and record.get("owner_id") != owner_id:
            raise RenewalAccessDenied(renewal_id)
        self.repo.delete(renewal_id)

    def due_count(self, *, owner_id: Optional[str], tier: str = "free") -> RenewalDueCount:
        overdue = due_soon = 0
        for m in self.list(owner_id=owner_id, tier=tier):
            if m.urgency == "overdue":
                overdue += 1
            elif m.urgency == "due_soon":
                due_soon += 1
        return RenewalDueCount(overdue=overdue, due_soon=due_soon, total_alerting=overdue + due_soon)
