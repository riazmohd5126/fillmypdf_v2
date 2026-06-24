"""
Document Approval Workflows
============================
Token-gated review requests stored as JSON under STORAGE_DIR/approvals/.

A requester creates an approval for a filled PDF; an external reviewer
receives a link with an unguessable token and can approve or reject without
an API key.
"""

from __future__ import annotations

import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from ..config import settings

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]
ApprovalDecision = Literal["approved", "rejected"]


class ApprovalRequest:
    """In-memory view of one approval request."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self._d = data

    @property
    def approval_id(self) -> str:
        return self._d["approval_id"]

    @property
    def status(self) -> ApprovalStatus:
        return self._d["status"]

    @property
    def title(self) -> str:
        return self._d.get("title", "Untitled")

    @property
    def pdf_filename(self) -> str:
        return self._d["pdf_filename"]

    @property
    def review_token(self) -> str:
        return self._d["review_token"]

    @property
    def reviewer_email(self) -> str:
        return self._d.get("reviewer_email", "")

    @property
    def reviewer_name(self) -> str:
        return self._d.get("reviewer_name", "")

    @property
    def requester_email(self) -> Optional[str]:
        return self._d.get("requester_email")

    @property
    def webhook_url(self) -> Optional[str]:
        return self._d.get("webhook_url")

    @property
    def webhook_secret(self) -> Optional[str]:
        return self._d.get("webhook_secret")

    @property
    def expires_at(self) -> str:
        return self._d["expires_at"]

    @property
    def created_at(self) -> str:
        return self._d["created_at"]

    @property
    def updated_at(self) -> str:
        return self._d["updated_at"]

    @property
    def decided_at(self) -> Optional[str]:
        return self._d.get("decided_at")

    @property
    def comment(self) -> Optional[str]:
        return self._d.get("comment")

    def is_expired(self) -> bool:
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= exp
        except (ValueError, TypeError):
            return False

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._d)


class ApprovalService:
    """CRUD + decision workflow for document approval requests."""

    @property
    def _dir(self) -> Path:
        p = settings.STORAGE_DIR / "approvals"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _path(self, approval_id: str) -> Path:
        return self._dir / f"{approval_id}.json"

    def _save(self, data: Dict[str, Any]) -> ApprovalRequest:
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._path(data["approval_id"]).write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )
        return ApprovalRequest(data)

    def _validate_token(self, record: ApprovalRequest, token: str) -> None:
        if not token or not hmac.compare_digest(record.review_token, token):
            raise PermissionError("Invalid review token.")

    def _ensure_actionable(self, record: ApprovalRequest) -> None:
        if record.status in ("approved", "rejected"):
            raise ValueError(f"Approval already decided (status: {record.status}).")
        if record.is_expired():
            raise ValueError("Approval link has expired.")

    # ── Create ─────────────────────────────────────────────────────────────

    def create(
        self,
        *,
        title: str,
        pdf_filename: str,
        reviewer_name: str = "",
        reviewer_email: str = "",
        requester_email: Optional[str] = None,
        webhook_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        created_by_key_id: Optional[str] = None,
        expires_in_hours: int = 168,
    ) -> ApprovalRequest:
        if not pdf_filename or not pdf_filename.strip():
            raise ValueError("pdf_filename is required.")
        if not reviewer_email and not reviewer_name:
            raise ValueError("At least reviewer_email or reviewer_name is required.")

        now = datetime.now(timezone.utc)
        approval_id = f"appr_{uuid.uuid4().hex[:16]}"
        expires_at = (now + timedelta(hours=max(1, expires_in_hours))).isoformat()

        data: Dict[str, Any] = {
            "approval_id": approval_id,
            "title": title.strip() or "Untitled",
            "pdf_filename": pdf_filename.strip(),
            "review_token": secrets.token_urlsafe(32),
            "status": "pending",
            "reviewer_name": reviewer_name.strip(),
            "reviewer_email": reviewer_email.strip(),
            "requester_email": (requester_email or "").strip() or None,
            "webhook_url": (webhook_url or "").strip() or None,
            "webhook_secret": (webhook_secret or "").strip() or None,
            "created_by_key_id": created_by_key_id,
            "expires_at": expires_at,
            "decided_at": None,
            "comment": None,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        return self._save(data)

    # ── Read ───────────────────────────────────────────────────────────────

    def get(self, approval_id: str) -> Optional[ApprovalRequest]:
        p = self._path(approval_id)
        if not p.exists():
            return None
        return ApprovalRequest(json.loads(p.read_text(encoding="utf-8")))

    def list_all(self, *, limit: int = 100) -> List[ApprovalRequest]:
        paths = sorted(
            self._dir.glob("appr_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        results: List[ApprovalRequest] = []
        for path in paths[:limit]:
            try:
                results.append(ApprovalRequest(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return results

    # ── Decide ─────────────────────────────────────────────────────────────

    def decide(
        self,
        approval_id: str,
        *,
        decision: ApprovalDecision,
        comment: Optional[str],
        token: str,
    ) -> ApprovalRequest:
        record = self.get(approval_id)
        if record is None:
            raise KeyError(f"Approval '{approval_id}' not found.")

        self._validate_token(record, token)
        self._ensure_actionable(record)

        data = record.to_dict()
        data["status"] = decision
        data["decided_at"] = datetime.now(timezone.utc).isoformat()
        data["comment"] = (comment or "").strip() or None
        return self._save(data)

    def verify_token_for_read(self, approval_id: str, token: str) -> ApprovalRequest:
        """Validate token for public read/download endpoints."""
        record = self.get(approval_id)
        if record is None:
            raise KeyError(f"Approval '{approval_id}' not found.")
        self._validate_token(record, token)
        if record.status == "pending" and record.is_expired():
            raise ValueError("Approval link has expired.")
        return record
