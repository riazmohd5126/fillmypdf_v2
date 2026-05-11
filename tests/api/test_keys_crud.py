"""
API Keys CRUD integration tests
================================
End-to-end exercise of /api/v1/keys/* (admin-only).
"""

import pytest


class TestKeyCreate:
    def test_create_free_key(self, client, auth_headers_admin):
        r = client.post(
            "/api/v1/keys/",
            headers=auth_headers_admin,
            json={"name": "Test Key", "tier": "free"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # Plaintext key returned exactly once
        assert body["key"].startswith(("fmp_live_", "fmp_test_"))
        assert body["name"] == "Test Key"
        assert body["tier"] == "free"
        assert body["request_count"] == 0
        assert body["revoked"] is False

    def test_create_pro_key(self, client, auth_headers_admin):
        r = client.post(
            "/api/v1/keys/",
            headers=auth_headers_admin,
            json={"name": "Pro", "tier": "pro", "owner": "team@example.com"},
        )
        assert r.status_code == 201
        assert r.json()["tier"] == "pro"
        assert r.json()["owner"] == "team@example.com"

    def test_create_invalid_tier_rejected(self, client, auth_headers_admin):
        r = client.post(
            "/api/v1/keys/",
            headers=auth_headers_admin,
            json={"name": "Bad", "tier": "platinum"},
        )
        assert r.status_code == 422

    def test_create_empty_name_rejected(self, client, auth_headers_admin):
        r = client.post(
            "/api/v1/keys/",
            headers=auth_headers_admin,
            json={"name": "", "tier": "free"},
        )
        assert r.status_code == 422


class TestKeyList:
    def test_list_includes_admin_key(self, client, auth_headers_admin):
        # Just the admin fixture key
        r = client.get("/api/v1/keys/", headers=auth_headers_admin)
        assert r.status_code == 200
        keys = r.json()
        assert len(keys) >= 1
        # No key should leak the plaintext key field
        assert all("key" not in k or k.get("key") is None for k in keys)
        # No hashes either
        assert all("key_hash" not in k for k in keys)

    def test_list_after_creates(self, client, auth_headers_admin):
        # Admin fixture already exists; create 2 more
        client.post("/api/v1/keys/", headers=auth_headers_admin,
                    json={"name": "K1", "tier": "free"})
        client.post("/api/v1/keys/", headers=auth_headers_admin,
                    json={"name": "K2", "tier": "pro"})

        r = client.get("/api/v1/keys/", headers=auth_headers_admin)
        assert r.status_code == 200
        names = {k["name"] for k in r.json()}
        assert "K1" in names and "K2" in names


class TestKeyGet:
    def test_get_by_id(self, client, auth_headers_admin):
        created = client.post(
            "/api/v1/keys/",
            headers=auth_headers_admin,
            json={"name": "Get Me", "tier": "free"},
        ).json()

        r = client.get(f"/api/v1/keys/{created['id']}", headers=auth_headers_admin)
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Get Me"
        # Plaintext should NOT appear in the read response
        assert "key" not in body or body.get("key") is None

    def test_get_missing_returns_404(self, client, auth_headers_admin):
        r = client.get("/api/v1/keys/key_doesnotexist", headers=auth_headers_admin)
        assert r.status_code == 404


class TestKeyRevoke:
    def test_revoke_marks_revoked(self, client, auth_headers_admin):
        created = client.post(
            "/api/v1/keys/",
            headers=auth_headers_admin,
            json={"name": "Revoke Me", "tier": "free"},
        ).json()

        r = client.post(
            f"/api/v1/keys/{created['id']}/revoke",
            headers=auth_headers_admin,
        )
        assert r.status_code == 200
        assert r.json()["revoked"] is True

    def test_revoked_key_cannot_authenticate(self, client, auth_headers_admin):
        created = client.post(
            "/api/v1/keys/",
            headers=auth_headers_admin,
            json={"name": "RevokeAuth", "tier": "free"},
        ).json()
        plain = created["key"]

        # Initially the new key works
        r = client.get("/api/v1/profiles/", headers={"X-API-Key": plain})
        assert r.status_code == 200

        # Revoke
        client.post(f"/api/v1/keys/{created['id']}/revoke", headers=auth_headers_admin)

        # After revocation, the same key is rejected
        r = client.get("/api/v1/profiles/", headers={"X-API-Key": plain})
        assert r.status_code == 403

    def test_revoke_missing_returns_404(self, client, auth_headers_admin):
        r = client.post("/api/v1/keys/missing/revoke", headers=auth_headers_admin)
        assert r.status_code == 404


class TestKeyDelete:
    def test_delete_removes(self, client, auth_headers_admin):
        created = client.post(
            "/api/v1/keys/",
            headers=auth_headers_admin,
            json={"name": "Del Me", "tier": "free"},
        ).json()

        r = client.delete(f"/api/v1/keys/{created['id']}", headers=auth_headers_admin)
        assert r.status_code == 204

        r = client.get(f"/api/v1/keys/{created['id']}", headers=auth_headers_admin)
        assert r.status_code == 404

    def test_delete_missing_returns_404(self, client, auth_headers_admin):
        r = client.delete("/api/v1/keys/missing", headers=auth_headers_admin)
        assert r.status_code == 404


class TestKeyEndToEndFlow:
    def test_create_use_revoke_delete(self, client, auth_headers_admin):
        # 1. Admin creates a free-tier key
        created = client.post(
            "/api/v1/keys/",
            headers=auth_headers_admin,
            json={"name": "E2E Key", "tier": "free"},
        ).json()
        key_id = created["id"]
        plain = created["key"]

        # 2. Use the key on a protected endpoint
        r = client.get("/api/v1/profiles/", headers={"X-API-Key": plain})
        assert r.status_code == 200

        # 3. Free tier => can create exactly 1 profile
        r = client.post(
            "/api/v1/profiles/",
            headers={"X-API-Key": plain},
            json={"name": "MyProfile"},
        )
        assert r.status_code == 201

        # 4. Admin lists keys & sees it
        r = client.get("/api/v1/keys/", headers=auth_headers_admin)
        assert key_id in [k["id"] for k in r.json()]

        # 5. Admin revokes
        client.post(f"/api/v1/keys/{key_id}/revoke", headers=auth_headers_admin)

        # 6. Revoked key fails
        r = client.get("/api/v1/profiles/", headers={"X-API-Key": plain})
        assert r.status_code == 403

        # 7. Admin deletes
        r = client.delete(f"/api/v1/keys/{key_id}", headers=auth_headers_admin)
        assert r.status_code == 204
