"""
Layer 3 – cache admin endpoint tests
======================================
Tests for GET /api/v1/batch/cache, DELETE /api/v1/batch/cache/{fingerprint},
and DELETE /api/v1/batch/cache.
"""
import json
import pytest
from unittest.mock import patch, MagicMock


BASE = "/api/v1/batch"


# ---------------------------------------------------------------------------
# Helpers: extract plain key string from fixture dict
# ---------------------------------------------------------------------------

def _plain(key_dict: dict) -> str:
    return key_dict["plain"]


# ---------------------------------------------------------------------------
# List cache entries
# ---------------------------------------------------------------------------

class TestListCacheEntries:

    def test_returns_entries_list(self, client, admin_api_key):
        fake_entries = [
            {"fingerprint": "abc123", "cached_at": "2026-01-01T00:00:00",
             "field_count": 5, "label_sample": []},
        ]
        with patch("fillmypdf.api.routes.batch_routes.TemplateCache") as MockCache:
            MockCache.return_value.list_entries.return_value = fake_entries
            resp = client.get(f"{BASE}/cache",
                              headers={"X-API-Key": _plain(admin_api_key)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["entries"][0]["fingerprint"] == "abc123"

    def test_empty_cache_returns_zero_total(self, client, admin_api_key):
        with patch("fillmypdf.api.routes.batch_routes.TemplateCache") as MockCache:
            MockCache.return_value.list_entries.return_value = []
            resp = client.get(f"{BASE}/cache",
                              headers={"X-API-Key": _plain(admin_api_key)})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_requires_admin_key_not_free(self, client, free_api_key):
        """Free-tier key should be rejected (403)."""
        with patch("fillmypdf.api.routes.batch_routes.TemplateCache") as MockCache:
            MockCache.return_value.list_entries.return_value = []
            resp = client.get(f"{BASE}/cache",
                              headers={"X-API-Key": _plain(free_api_key)})
        assert resp.status_code == 403

    def test_no_key_returns_401(self, client):
        resp = client.get(f"{BASE}/cache")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Invalidate single entry
# ---------------------------------------------------------------------------

class TestInvalidateCacheEntry:

    def test_returns_204_on_success(self, client, admin_api_key):
        with patch("fillmypdf.api.routes.batch_routes.TemplateCache") as MockCache:
            MockCache.return_value.invalidate.return_value = True
            resp = client.delete(f"{BASE}/cache/deadbeef1234",
                                 headers={"X-API-Key": _plain(admin_api_key)})
        assert resp.status_code == 204

    def test_returns_404_when_not_found(self, client, admin_api_key):
        with patch("fillmypdf.api.routes.batch_routes.TemplateCache") as MockCache:
            MockCache.return_value.invalidate.return_value = False
            resp = client.delete(f"{BASE}/cache/doesnotexist",
                                 headers={"X-API-Key": _plain(admin_api_key)})
        assert resp.status_code == 404

    def test_requires_admin_key_not_free(self, client, free_api_key):
        with patch("fillmypdf.api.routes.batch_routes.TemplateCache") as MockCache:
            MockCache.return_value.invalidate.return_value = True
            resp = client.delete(f"{BASE}/cache/abc",
                                 headers={"X-API-Key": _plain(free_api_key)})
        assert resp.status_code == 403

    def test_no_key_returns_401(self, client):
        resp = client.delete(f"{BASE}/cache/abc")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Clear entire cache
# ---------------------------------------------------------------------------

class TestClearCache:

    def test_returns_204(self, client, admin_api_key):
        fake_entries = [{"fingerprint": "fp1"}, {"fingerprint": "fp2"}]
        with patch("fillmypdf.api.routes.batch_routes.TemplateCache") as MockCache:
            instance = MockCache.return_value
            instance.list_entries.return_value = fake_entries
            instance.invalidate.return_value = True
            resp = client.delete(f"{BASE}/cache",
                                 headers={"X-API-Key": _plain(admin_api_key)})
        assert resp.status_code == 204

    def test_calls_invalidate_for_each_entry(self, client, admin_api_key):
        fake_entries = [{"fingerprint": "fp1"}, {"fingerprint": "fp2"}]
        with patch("fillmypdf.api.routes.batch_routes.TemplateCache") as MockCache:
            instance = MockCache.return_value
            instance.list_entries.return_value = fake_entries
            instance.invalidate.return_value = True
            client.delete(f"{BASE}/cache",
                          headers={"X-API-Key": _plain(admin_api_key)})
        assert instance.invalidate.call_count == 2

    def test_empty_cache_still_returns_204(self, client, admin_api_key):
        with patch("fillmypdf.api.routes.batch_routes.TemplateCache") as MockCache:
            instance = MockCache.return_value
            instance.list_entries.return_value = []
            instance.invalidate.return_value = True
            resp = client.delete(f"{BASE}/cache",
                                 headers={"X-API-Key": _plain(admin_api_key)})
        assert resp.status_code == 204

    def test_requires_admin_key_not_free(self, client, free_api_key):
        resp = client.delete(f"{BASE}/cache",
                             headers={"X-API-Key": _plain(free_api_key)})
        assert resp.status_code == 403
