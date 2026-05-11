"""
Unit tests for fillmypdf.repositories.api_key_repository.APIKeyRepository
=========================================================================
Bcrypt hash, verify, persist, lookup-by-prefix, revocation.
"""

import pytest

from fillmypdf.repositories.api_key_repository import APIKeyRepository


class TestHashAndVerify:
    def test_round_trip(self):
        h = APIKeyRepository.hash_key("fmp_live_ABC123XYZ")
        assert h.startswith("$2b$")
        assert APIKeyRepository.verify_key("fmp_live_ABC123XYZ", h) is True

    def test_wrong_key_fails(self):
        h = APIKeyRepository.hash_key("fmp_live_RIGHT")
        assert APIKeyRepository.verify_key("fmp_live_WRONG", h) is False

    def test_corrupt_hash_does_not_raise(self):
        # Should return False, not throw
        assert APIKeyRepository.verify_key("anything", "not-a-bcrypt-hash") is False


class TestRepositoryCRUD:
    def test_save_and_get(self):
        repo = APIKeyRepository()
        record = {
            "id": "key_abc123",
            "name": "Test",
            "tier": "free",
            "owner": None,
            "prefix": "fmp_live_AB",
            "key_hash": "$2b$12$dummyhash",
            "created_at": "2026-01-01T00:00:00",
            "last_used_at": None,
            "request_count": 0,
            "revoked": False,
        }
        assert repo.save(record) is True

        loaded = repo.get("key_abc123")
        assert loaded is not None
        assert loaded["name"] == "Test"

    def test_get_missing_returns_none(self):
        repo = APIKeyRepository()
        assert repo.get("does-not-exist") is None

    def test_list_all_returns_empty_initially(self):
        repo = APIKeyRepository()
        assert repo.list_all() == []

    def test_list_all_returns_saved(self):
        repo = APIKeyRepository()
        for i in range(3):
            repo.save({
                "id": f"key_{i}",
                "name": f"Key {i}",
                "tier": "free",
                "prefix": "fmp_live_AB",
                "key_hash": "$2b$12$x",
                "created_at": f"2026-01-0{i+1}T00:00:00",
                "request_count": 0,
                "revoked": False,
                "last_used_at": None,
                "owner": None,
            })
        records = repo.list_all()
        assert len(records) == 3
        # Sorted by created_at desc
        assert records[0]["id"] == "key_2"

    def test_delete_removes(self):
        repo = APIKeyRepository()
        repo.save({
            "id": "key_xyz",
            "name": "Del me",
            "tier": "free",
            "prefix": "fmp_live_AB",
            "key_hash": "$2b$12$x",
            "created_at": "2026-01-01T00:00:00",
            "request_count": 0, "revoked": False,
            "last_used_at": None, "owner": None,
        })
        assert repo.delete("key_xyz") is True
        assert repo.get("key_xyz") is None
        assert repo.delete("key_xyz") is False  # idempotent

    def test_touch_increments_count(self):
        repo = APIKeyRepository()
        repo.save({
            "id": "key_touch",
            "name": "T",
            "tier": "free",
            "prefix": "fmp_live_AB",
            "key_hash": "$2b$12$x",
            "created_at": "2026-01-01T00:00:00",
            "request_count": 0, "revoked": False,
            "last_used_at": None, "owner": None,
        })
        repo.touch("key_touch")
        repo.touch("key_touch")
        assert repo.get("key_touch")["request_count"] == 2
        assert repo.get("key_touch")["last_used_at"] is not None


class TestFindByPlaintext:
    """Validates the prefix-narrowed bcrypt lookup."""

    def test_find_by_plaintext_match(self):
        repo = APIKeyRepository()
        plain = "fmp_live_ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        repo.save({
            "id": "key_match",
            "name": "Match",
            "tier": "pro",
            "prefix": plain[:12],
            "key_hash": APIKeyRepository.hash_key(plain),
            "created_at": "2026-01-01T00:00:00",
            "request_count": 0, "revoked": False,
            "last_used_at": None, "owner": None,
        })
        found = repo.find_by_plaintext(plain)
        assert found is not None
        assert found["id"] == "key_match"
        assert found["tier"] == "pro"

    def test_find_by_plaintext_wrong_key_no_match(self):
        repo = APIKeyRepository()
        plain = "fmp_live_ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
        repo.save({
            "id": "key_x",
            "name": "X",
            "tier": "free",
            "prefix": plain[:12],
            "key_hash": APIKeyRepository.hash_key(plain),
            "created_at": "2026-01-01T00:00:00",
            "request_count": 0, "revoked": False,
            "last_used_at": None, "owner": None,
        })
        # Same prefix but different body
        wrong = plain[:12] + "99999999999999999999999999999999"
        assert repo.find_by_plaintext(wrong) is None

    def test_revoked_key_not_found(self):
        repo = APIKeyRepository()
        plain = "fmp_live_REVOKEDKEYABCDEFGHIJ234567890XY"
        repo.save({
            "id": "key_rev",
            "name": "Revoked",
            "tier": "free",
            "prefix": plain[:12],
            "key_hash": APIKeyRepository.hash_key(plain),
            "created_at": "2026-01-01T00:00:00",
            "request_count": 0, "revoked": True,   # ← revoked
            "last_used_at": None, "owner": None,
        })
        assert repo.find_by_plaintext(plain) is None

    def test_too_short_key_returns_none(self):
        repo = APIKeyRepository()
        assert repo.find_by_plaintext("short") is None
        assert repo.find_by_plaintext("") is None
