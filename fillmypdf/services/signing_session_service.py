"""
Multi-Party Signing Sessions
=============================
Manages sequential multi-signer workflows stored as JSON files under
STORAGE_DIR/signing_sessions/.

A session tracks:
  - The base PDF (stored in OUTPUT_DIR, referenced by filename)
  - An ordered list of signers, each with:
      - name, email, field_key (from template manifest), status
  - Overall session status: pending | in_progress | complete | cancelled
  - Each completed signer step records the signed PDF filename and audit_id

Sessions advance one signer at a time. When all signers have signed,
the final PDF (with all overlays applied sequentially) is the output.

This is a workflow-level signing record, NOT a cryptographic multi-sig.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from ..config import settings


SessionStatus = Literal["pending", "in_progress", "complete", "cancelled"]
SignerStatus = Literal["pending", "signed", "declined"]


class SigningSession:
    """In-memory view of one signing session. Use SigningSessionService to persist."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self._d = data

    # ── Accessors ──────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._d["session_id"]

    @property
    def status(self) -> SessionStatus:
        return self._d["status"]

    @property
    def title(self) -> str:
        return self._d.get("title", "Untitled")

    @property
    def signers(self) -> List[Dict[str, Any]]:
        return self._d["signers"]

    @property
    def current_signer_index(self) -> int:
        return self._d.get("current_signer_index", 0)

    @property
    def current_pdf_filename(self) -> str:
        """Filename of the most-recently-signed PDF (or base PDF if none signed yet)."""
        return self._d["current_pdf_filename"]

    @property
    def base_pdf_filename(self) -> str:
        return self._d["base_pdf_filename"]

    @property
    def template_id(self) -> Optional[str]:
        return self._d.get("template_id")

    @property
    def created_at(self) -> str:
        return self._d["created_at"]

    @property
    def updated_at(self) -> str:
        return self._d["updated_at"]

    @property
    def final_pdf_filename(self) -> Optional[str]:
        return self._d.get("final_pdf_filename")

    def current_signer(self) -> Optional[Dict[str, Any]]:
        idx = self.current_signer_index
        if idx < len(self.signers):
            return self.signers[idx]
        return None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._d)


class SigningSessionService:
    """CRUD + workflow advancement for multi-party signing sessions."""

    @property
    def _dir(self) -> Path:
        p = settings.STORAGE_DIR / "signing_sessions"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def _save(self, data: Dict[str, Any]) -> SigningSession:
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._path(data["session_id"]).write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )
        return SigningSession(data)

    # ── Create ─────────────────────────────────────────────────────────────

    def create(
        self,
        *,
        title: str,
        base_pdf_filename: str,
        signers: List[Dict[str, Any]],
        template_id: Optional[str] = None,
        created_by_key_id: Optional[str] = None,
    ) -> SigningSession:
        """
        Create a new session.

        Each signer dict must have: name (str), email (str).
        Optional per-signer: field_key (str), x_pct, y_pct, width_pct, height_pct, page_index.
        """
        if not signers:
            raise ValueError("At least one signer is required.")
        if len(signers) > 10:
            raise ValueError("Maximum 10 signers per session.")

        now = datetime.now(timezone.utc).isoformat()
        session_id = f"sess_{uuid.uuid4().hex[:16]}"

        normalised_signers = []
        for i, s in enumerate(signers):
            if not s.get("name") and not s.get("email"):
                raise ValueError(f"Signer {i + 1} must have at least a name or email.")
            normalised_signers.append(
                {
                    "index": i,
                    "name": s.get("name", ""),
                    "email": s.get("email", ""),
                    "field_key": s.get("field_key"),
                    "page_index": s.get("page_index", 0),
                    "x_pct": s.get("x_pct", 55.0),
                    "y_pct": s.get("y_pct", 5.0),
                    "width_pct": s.get("width_pct", 40.0),
                    "height_pct": s.get("height_pct", 12.0),
                    "status": "pending",
                    "signed_at": None,
                    "audit_id": None,
                    "signed_pdf_filename": None,
                }
            )

        data: Dict[str, Any] = {
            "session_id": session_id,
            "title": title.strip() or "Untitled",
            "status": "pending",
            "template_id": template_id,
            "base_pdf_filename": base_pdf_filename,
            "current_pdf_filename": base_pdf_filename,
            "current_signer_index": 0,
            "signers": normalised_signers,
            "final_pdf_filename": None,
            "created_by_key_id": created_by_key_id,
            "created_at": now,
            "updated_at": now,
        }
        return self._save(data)

    # ── Read ───────────────────────────────────────────────────────────────

    def get(self, session_id: str) -> Optional[SigningSession]:
        p = self._path(session_id)
        if not p.exists():
            return None
        return SigningSession(json.loads(p.read_text(encoding="utf-8")))

    def list_all(self, *, limit: int = 100) -> List[SigningSession]:
        paths = sorted(self._dir.glob("sess_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        sessions = []
        for path in paths[:limit]:
            try:
                sessions.append(SigningSession(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sessions

    # ── Advance (record one signer's signature) ────────────────────────────

    def record_signature(
        self,
        session_id: str,
        *,
        signer_index: int,
        signed_pdf_filename: str,
        audit_id: str,
    ) -> SigningSession:
        """Mark signer at signer_index as signed and advance the session."""
        sess = self.get(session_id)
        if sess is None:
            raise KeyError(f"Session '{session_id}' not found.")
        if sess.status == "complete":
            raise ValueError("Session is already complete.")
        if sess.status == "cancelled":
            raise ValueError("Session has been cancelled.")
        if signer_index != sess.current_signer_index:
            raise ValueError(
                f"Out-of-order signing: expected signer {sess.current_signer_index}, got {signer_index}."
            )

        data = sess.to_dict()
        data["signers"][signer_index]["status"] = "signed"
        data["signers"][signer_index]["signed_at"] = datetime.now(timezone.utc).isoformat()
        data["signers"][signer_index]["audit_id"] = audit_id
        data["signers"][signer_index]["signed_pdf_filename"] = signed_pdf_filename
        data["current_pdf_filename"] = signed_pdf_filename

        next_index = signer_index + 1
        if next_index >= len(data["signers"]):
            data["status"] = "complete"
            data["final_pdf_filename"] = signed_pdf_filename
            data["current_signer_index"] = next_index
        else:
            data["status"] = "in_progress"
            data["current_signer_index"] = next_index

        return self._save(data)

    # ── Cancel ─────────────────────────────────────────────────────────────

    def cancel(self, session_id: str) -> SigningSession:
        sess = self.get(session_id)
        if sess is None:
            raise KeyError(f"Session '{session_id}' not found.")
        data = sess.to_dict()
        data["status"] = "cancelled"
        return self._save(data)
