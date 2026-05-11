"""
API Key Repository
==================
Persistence for API keys.

Storage layout:
  storage/api_keys/<key_id>.json    — public metadata + bcrypt hash of the key

The plaintext key is NEVER stored. Lookup is O(N) on number of keys (fine for
the expected scale; can be swapped for a real DB index later).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import bcrypt

from ..config import settings


# Bcrypt cost factor.
#   - Production: 12 (~250ms per hash — strong)
#   - Tests:       4 (~5ms — set BCRYPT_ROUNDS=4 in conftest)
_BCRYPT_ROUNDS = int(os.environ.get("BCRYPT_ROUNDS", "12"))


class APIKeyRepository:
    """File-based repository for API keys."""

    def __init__(self):
        # Don't cache — re-read settings on every access so that runtime
        # settings updates (and pytest monkeypatching) are picked up by
        # any singletons that hold a reference to this repo.
        self.storage_dir  # ensures the dir exists on construction

    @property
    def storage_dir(self) -> Path:
        path = settings.STORAGE_DIR / "api_keys"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------

    @staticmethod
    def hash_key(plain_key: str) -> str:
        """Bcrypt-hash an API key. Cost factor configurable via BCRYPT_ROUNDS."""
        return bcrypt.hashpw(
            plain_key.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
        ).decode("utf-8")

    @staticmethod
    def verify_key(plain_key: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(plain_key.encode("utf-8"), hashed.encode("utf-8"))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def _file_path(self, key_id: str) -> Path:
        return self.storage_dir / f"{key_id}.json"

    def save(self, record: Dict) -> bool:
        try:
            with open(self._file_path(record["id"]), "w") as fh:
                json.dump(record, fh, indent=2, default=str)
            return True
        except Exception as e:
            print(f"Error saving API key: {e}")
            return False

    def get(self, key_id: str) -> Optional[Dict]:
        path = self._file_path(key_id)
        if not path.exists():
            return None
        try:
            with open(path, "r") as fh:
                return json.load(fh)
        except Exception as e:
            print(f"Error loading API key {key_id}: {e}")
            return None

    def list_all(self) -> List[Dict]:
        records: List[Dict] = []
        for path in self.storage_dir.glob("*.json"):
            try:
                with open(path, "r") as fh:
                    records.append(json.load(fh))
            except Exception as e:
                print(f"Skipping corrupt key file {path.name}: {e}")
                continue
        return sorted(records, key=lambda r: r.get("created_at", ""), reverse=True)

    def delete(self, key_id: str) -> bool:
        path = self._file_path(key_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except Exception as e:
            print(f"Error deleting API key {key_id}: {e}")
            return False

    # ------------------------------------------------------------------
    # Lookup by plaintext key
    # ------------------------------------------------------------------

    def find_by_plaintext(self, plain_key: str) -> Optional[Dict]:
        """
        Validate a plaintext API key against all stored hashes.
        Returns the key record (with metadata) if it matches; else None.
        We narrow the search by the public 'prefix' field first to avoid
        bcrypt-verifying every key in storage.
        """
        if not plain_key or len(plain_key) < 12:
            return None
        prefix = plain_key[:12]
        for record in self.list_all():
            if record.get("prefix") != prefix:
                continue
            if record.get("revoked"):
                continue
            if self.verify_key(plain_key, record.get("key_hash", "")):
                return record
        return None

    # ------------------------------------------------------------------
    # Usage tracking
    # ------------------------------------------------------------------

    def touch(self, key_id: str) -> None:
        """Increment request_count and update last_used_at — best-effort."""
        record = self.get(key_id)
        if not record:
            return
        record["request_count"] = record.get("request_count", 0) + 1
        record["last_used_at"] = datetime.now().isoformat()
        self.save(record)
