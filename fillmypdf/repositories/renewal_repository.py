"""
Renewal Repository
===================
Data access layer for PA renewal-tracking records. Same JSON-file-per-
record pattern as ProfileRepository, with the two PHI-bearing fields
(patient_name, member_id) encrypted at rest the same way profile data is.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings
from ..utils.encryption import Encryption

_ENCRYPTED_FIELDS = ("patient_name", "member_id")


class RenewalRepository:
    def __init__(self) -> None:
        settings.RENEWALS_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def storage_dir(self) -> Path:
        path = settings.RENEWALS_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def encryption_key(self) -> str:
        return settings.PROFILES_ENCRYPTION_KEY

    @property
    def encryption_enabled(self) -> bool:
        return settings.PROFILES_ENCRYPTION_ENABLED

    def _path(self, renewal_id: str) -> Path:
        return self.storage_dir / f"{renewal_id}.json"

    def _encrypt(self, record: Dict) -> Dict:
        if not self.encryption_enabled:
            return record
        out = dict(record)
        for field in _ENCRYPTED_FIELDS:
            value = out.get(field)
            if value:
                out[f"{field}_encrypted"] = Encryption.encrypt(str(value), self.encryption_key)
                out.pop(field, None)
        return out

    def _decrypt(self, record: Dict) -> Dict:
        if not self.encryption_enabled:
            return record
        out = dict(record)
        for field in _ENCRYPTED_FIELDS:
            enc_key = f"{field}_encrypted"
            if enc_key in out:
                try:
                    out[field] = Encryption.decrypt(out[enc_key], self.encryption_key)
                except Exception:
                    out[field] = "[DECRYPTION_ERROR]"
                out.pop(enc_key, None)
        return out

    def save(self, record: Dict) -> bool:
        try:
            with open(self._path(record["id"]), "w") as f:
                json.dump(self._encrypt(record), f, indent=2, default=str)
            return True
        except Exception as e:
            print(f"Error saving renewal {record.get('id')}: {e}")
            return False

    def get(self, renewal_id: str) -> Optional[Dict]:
        path = self._path(renewal_id)
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                return self._decrypt(json.load(f))
        except Exception as e:
            print(f"Error loading renewal {renewal_id}: {e}")
            return None

    def list_all(self) -> List[Dict]:
        out = []
        for path in self.storage_dir.glob("*.json"):
            try:
                with open(path, "r") as f:
                    out.append(self._decrypt(json.load(f)))
            except Exception as e:
                print(f"Skipping corrupt renewal file {path.name}: {e}")
        return sorted(out, key=lambda r: r.get("created_at", ""), reverse=True)

    def list_for_owner(self, owner_id: Optional[str]) -> List[Dict]:
        if not owner_id:
            return []
        return [r for r in self.list_all() if r.get("owner_id") == owner_id]

    def delete(self, renewal_id: str) -> bool:
        path = self._path(renewal_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except Exception as e:
            print(f"Error deleting renewal {renewal_id}: {e}")
            return False

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
