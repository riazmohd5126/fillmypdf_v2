"""HttpOnly session tokens for clinic login."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..config import settings


class SessionRepository:
    @property
    def storage_dir(self) -> Path:
        path = settings.STORAGE_DIR / "sessions"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _token_name(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:40]

    def _path(self, token: str) -> Path:
        return self.storage_dir / f"{self._token_name(token)}.json"

    def create(self, user_id: str, *, ttl_days: int) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=max(1, int(ttl_days)))
        record = {
            "user_id": user_id,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }
        self._path(token).write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        return token

    def get(self, token: str) -> Optional[dict]:
        if not token:
            return None
        p = self._path(token)
        if not p.exists():
            return None
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        exp = rec.get("expires_at") or ""
        try:
            expires = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except Exception:
            self.delete(token)
            return None
        if expires < datetime.now(timezone.utc):
            self.delete(token)
            return None
        return rec

    def delete(self, token: str) -> bool:
        p = self._path(token)
        if not p.exists():
            return False
        try:
            p.unlink()
            return True
        except Exception:
            return False
