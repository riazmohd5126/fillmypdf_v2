"""
User accounts (email/password) — disk JSON, same pattern as API keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings
from .api_key_repository import APIKeyRepository


class UserRepository:
    @property
    def storage_dir(self) -> Path:
        path = settings.STORAGE_DIR / "users"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _path(self, user_id: str) -> Path:
        return self.storage_dir / f"{user_id}.json"

    def _index_path(self) -> Path:
        return self.storage_dir / "_email_index.json"

    def _load_index(self) -> Dict[str, str]:
        p = self._index_path()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_index(self, index: Dict[str, str]) -> None:
        self._index_path().write_text(
            json.dumps(index, indent=2), encoding="utf-8"
        )

    def save(self, record: Dict) -> bool:
        try:
            self._path(record["id"]).write_text(
                json.dumps(record, indent=2, default=str), encoding="utf-8"
            )
            index = self._load_index()
            email = (record.get("email") or "").strip().lower()
            if email:
                index[email] = record["id"]
                self._save_index(index)
            return True
        except Exception as e:
            print(f"Error saving user: {e}")
            return False

    def get(self, user_id: str) -> Optional[Dict]:
        p = self._path(user_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Error loading user {user_id}: {e}")
            return None

    def get_by_email(self, email: str) -> Optional[Dict]:
        email = (email or "").strip().lower()
        if not email:
            return None
        uid = self._load_index().get(email)
        if uid:
            rec = self.get(uid)
            if rec:
                return rec
        for rec in self.list_all():
            if (rec.get("email") or "").strip().lower() == email:
                return rec
        return None

    def list_all(self) -> List[Dict]:
        rows: List[Dict] = []
        for path in self.storage_dir.glob("usr_*.json"):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return rows

    @staticmethod
    def hash_password(plain: str) -> str:
        return APIKeyRepository.hash_key(plain)

    @staticmethod
    def verify_password(plain: str, hashed: str) -> bool:
        return APIKeyRepository.verify_key(plain, hashed)
