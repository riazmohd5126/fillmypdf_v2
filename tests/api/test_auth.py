"""
API auth tests
==============
Verify the X-API-Key header gate on protected endpoints.

Public endpoints (no auth):
  /, /health, /usage, /docs, /openapi.json

Protected endpoints (require any valid key):
  /api/v1/profiles/*, /api/v1/batch/*

Admin-only endpoints:
  /api/v1/keys/*
"""

import pytest


class TestPublicEndpoints:
    def test_root_no_auth(self, client):
        r = client.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["features"]["authentication"] is True

    def test_health_no_auth(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_usage_no_auth(self, client):
        r = client.get("/usage")
        assert r.status_code == 200

    def test_docs_no_auth(self, client):
        r = client.get("/docs")
        assert r.status_code == 200

    def test_openapi_json_no_auth(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert spec["info"]["title"] == "FillMyPDF"


class TestMissingKey:
    """Endpoints that require auth must return 401 without a key."""

    def test_profiles_list_requires_key(self, client):
        r = client.get("/api/v1/profiles/")
        assert r.status_code == 401
        assert "API key" in r.json()["detail"]

    def test_profiles_create_requires_key(self, client):
        r = client.post("/api/v1/profiles/", json={"name": "X"})
        assert r.status_code == 401

    def test_keys_list_requires_key(self, client):
        r = client.get("/api/v1/keys/")
        assert r.status_code == 401

    def test_keys_create_requires_key(self, client):
        r = client.post("/api/v1/keys/", json={"name": "X", "tier": "free"})
        assert r.status_code == 401


class TestInvalidKey:
    """A bogus key should be rejected with 403."""

    def test_random_garbage_rejected(self, client):
        r = client.get("/api/v1/profiles/", headers={"X-API-Key": "fmp_live_GARBAGE"})
        assert r.status_code == 403
        assert "Invalid" in r.json()["detail"]

    def test_short_key_rejected(self, client):
        r = client.get("/api/v1/profiles/", headers={"X-API-Key": "x"})
        assert r.status_code == 403


class TestValidKey:
    """A valid free-tier key should be accepted on protected endpoints."""

    def test_valid_key_passes_profiles_list(self, client, auth_headers_free):
        r = client.get("/api/v1/profiles/", headers=auth_headers_free)
        assert r.status_code == 200

    def test_authorization_bearer_format_works(self, client, free_api_key):
        r = client.get(
            "/api/v1/profiles/",
            headers={"Authorization": f"Bearer {free_api_key['plain']}"}
        )
        assert r.status_code == 200


class TestAdminGate:
    """Only admin tier can access /api/v1/keys/*"""

    def test_free_key_blocked_from_keys_list(self, client, auth_headers_free):
        r = client.get("/api/v1/keys/", headers=auth_headers_free)
        assert r.status_code == 403
        assert "Admin" in r.json()["detail"]

    def test_pro_key_blocked_from_keys_list(self, client, auth_headers_pro):
        r = client.get("/api/v1/keys/", headers=auth_headers_pro)
        assert r.status_code == 403

    def test_admin_key_allowed(self, client, auth_headers_admin):
        r = client.get("/api/v1/keys/", headers=auth_headers_admin)
        assert r.status_code == 200
        # Should at least include the admin key itself
        assert len(r.json()) >= 1


class TestRevocation:
    def test_revoked_key_rejected(self, client, free_api_key, api_key_service):
        # First, revoke the key
        api_key_service.revoke_key(free_api_key["id"])

        r = client.get("/api/v1/profiles/", headers={"X-API-Key": free_api_key["plain"]})
        assert r.status_code == 403


class TestUsageTracking:
    def test_request_count_increments(self, client, free_api_key, api_key_service):
        before = api_key_service.get_key(free_api_key["id"]).request_count

        client.get("/api/v1/profiles/", headers={"X-API-Key": free_api_key["plain"]})
        client.get("/api/v1/profiles/", headers={"X-API-Key": free_api_key["plain"]})

        after = api_key_service.get_key(free_api_key["id"]).request_count
        assert after >= before + 2
