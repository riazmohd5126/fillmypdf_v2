"""
API Key Service
===============
Business logic for API keys: generation, validation, lifecycle.

Key format:
  fmp_<env>_<32 base32 chars>
    fmp        — vendor prefix
    env        — 'live' for production keys, 'test' for non-prod
    32 chars   — 160 bits of entropy (base32, no ambiguous chars)

Example:
  fmp_live_K7Q3W9X2N4M5P8R6T1V0J3D5F7H9L2C4
"""

from __future__ import annotations

import secrets
import string
import uuid
from datetime import datetime
from typing import List, Optional

from ..config import settings
from ..models import APIKey, APIKeyCreate, APIKeyCreateResponse, Tier
from ..repositories.api_key_repository import APIKeyRepository


# Base32 minus 0/1/8/L/I/O — common readability rules
_KEY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_KEY_LENGTH = 32


class APIKeyService:
    """Manage API key lifecycle."""

    def __init__(self):
        self.repository = APIKeyRepository()

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_plaintext_key() -> str:
        """Generate a fresh plaintext API key with the 'fmp_<env>_' prefix."""
        env = "live" if not settings.DEBUG else "test"
        random_part = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(_KEY_LENGTH))
        return f"fmp_{env}_{random_part}"

    def create_key(self, payload: APIKeyCreate) -> APIKeyCreateResponse:
        """Generate, hash, and persist a new key. Returns the plaintext exactly once."""
        plain_key = self._generate_plaintext_key()
        key_id = f"key_{uuid.uuid4().hex[:12]}"

        record = {
            "id": key_id,
            "name": payload.name,
            "tier": payload.tier,
            "owner": payload.owner,
            "prefix": plain_key[:12],
            "key_hash": APIKeyRepository.hash_key(plain_key),
            "created_at": datetime.now().isoformat(),
            "last_used_at": None,
            "request_count": 0,
            "revoked": False,
        }

        if not self.repository.save(record):
            raise RuntimeError("Failed to persist API key")

        return APIKeyCreateResponse(
            **{k: v for k, v in record.items() if k != "key_hash"},
            key=plain_key,
        )

    # ------------------------------------------------------------------
    # Validation (used by auth dependency)
    # ------------------------------------------------------------------

    def validate(self, plain_key: str) -> Optional[dict]:
        """Return the full key record if valid+active, else None."""
        record = self.repository.find_by_plaintext(plain_key)
        if record and not record.get("revoked"):
            return record
        return None

    def touch(self, key_id: str) -> None:
        self.repository.touch(key_id)

    # ------------------------------------------------------------------
    # Listing / revoking
    # ------------------------------------------------------------------

    def list_keys(self) -> List[APIKey]:
        return [self._to_model(r) for r in self.repository.list_all()]

    def get_key(self, key_id: str) -> Optional[APIKey]:
        record = self.repository.get(key_id)
        return self._to_model(record) if record else None

    def revoke_key(self, key_id: str) -> bool:
        record = self.repository.get(key_id)
        if not record:
            return False
        record["revoked"] = True
        return self.repository.save(record)

    def delete_key(self, key_id: str) -> bool:
        return self.repository.delete(key_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_model(record: dict) -> APIKey:
        return APIKey(
            id=record["id"],
            name=record["name"],
            tier=record["tier"],
            owner=record.get("owner"),
            prefix=record["prefix"],
            created_at=datetime.fromisoformat(record["created_at"]),
            last_used_at=(
                datetime.fromisoformat(record["last_used_at"])
                if record.get("last_used_at") else None
            ),
            request_count=record.get("request_count", 0),
            revoked=record.get("revoked", False),
        )

    # ------------------------------------------------------------------
    # Bootstrap — call on startup if no keys exist
    # ------------------------------------------------------------------

    def bootstrap_admin_key_if_empty(self) -> Optional[APIKeyCreateResponse]:
        """
        If no keys exist yet, create an admin key and return it (plaintext).
        Otherwise return None. The plaintext is printed by main.py to the
        terminal at startup so the operator can save it.
        """
        if self.repository.list_all():
            return None
        return self.create_key(APIKeyCreate(
            name="Bootstrap Admin Key",
            tier="admin",
            owner="bootstrap",
        ))
