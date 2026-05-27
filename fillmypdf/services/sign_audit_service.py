"""
Signing audit log (workflow trail — not cryptographic / ESIGN legal proof).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import settings


class SignAuditService:
    """Append-only JSONL audit entries under STORAGE_DIR/sign_audit/."""

    @property
    def _dir(self) -> Path:
        p = settings.STORAGE_DIR / "sign_audit"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def _log_path(self) -> Path:
        return self._dir / "events.jsonl"

    def record(
        self,
        *,
        output_filename: str,
        download_url: str,
        page_index: int,
        signature_mode: str,
        signer_name: Optional[str] = None,
        signer_email: Optional[str] = None,
        api_key_id: Optional[str] = None,
        client_ip: Optional[str] = None,
        placement: Optional[Dict[str, float]] = None,
    ) -> str:
        audit_id = f"sig_{uuid.uuid4().hex[:16]}"
        entry: Dict[str, Any] = {
            "audit_id": audit_id,
            "event": "signature.applied",
            "at": datetime.now(timezone.utc).isoformat(),
            "output_filename": output_filename,
            "download_url": download_url,
            "page_index": page_index,
            "signature_mode": signature_mode,
            "signer_name": signer_name,
            "signer_email": signer_email,
            "api_key_id": api_key_id,
            "client_ip": client_ip,
            "placement_pct": placement or {},
            "disclaimer": "Visual overlay only — not PAdES or tamper-evident legal signing.",
        }
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        return audit_id

    def list_recent(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        if not self._log_path.exists():
            return []
        lines = self._log_path.read_text(encoding="utf-8").strip().splitlines()
        out: List[Dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        out.reverse()
        return out
