"""
Profiles CRUD integration tests
================================
End-to-end exercise of /api/v1/profiles/* using the FastAPI TestClient.

A free-tier API key is used to gate access. The free-tier limit (1 profile)
is also asserted.
"""

import pytest


class TestProfileCreate:
    def test_create_minimal_profile(self, client, auth_headers_free):
        r = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={"name": "My Profile", "profile_type": "personal"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "My Profile"
        assert body["profile_type"] == "personal"
        assert "id" in body
        assert body["usage_count"] == 0

    def test_create_with_data_fields(self, client, auth_headers_free):
        r = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={
                "name": "Patient Smith",
                "profile_type": "personal",
                "data": {
                    "first_name": "John",
                    "last_name": "Smith",
                    "dob": "1980-05-15",
                    "ssn": "123-45-6789",
                    "email": "john@example.com",
                },
            },
        )
        assert r.status_code == 201
        # Sensitive fields should NOT appear in plaintext in the preview
        preview = r.json().get("data_preview", {})
        assert "first_name" in preview
        # SSN should either be missing or masked
        assert "ssn" not in preview or preview.get("ssn", "").startswith("***")

    def test_create_empty_name_rejected(self, client, auth_headers_free):
        r = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={"name": "", "profile_type": "personal"},
        )
        assert r.status_code == 422  # Pydantic validation

    def test_invalid_profile_type_rejected(self, client, auth_headers_free):
        r = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={"name": "X", "profile_type": "alien"},
        )
        assert r.status_code == 422


class TestProfileTierLimits:
    def test_free_tier_blocked_at_second_profile(self, client, auth_headers_free):
        first = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={"name": "First"},
        )
        assert first.status_code == 201

        second = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={"name": "Second"},
        )
        assert second.status_code == 400
        assert "limit" in second.json()["detail"].lower()

    def test_pro_tier_unlimited(self, client, auth_headers_pro):
        for i in range(3):
            r = client.post(
                "/api/v1/profiles/",
                headers=auth_headers_pro,
                json={"name": f"Profile {i}"},
            )
            assert r.status_code == 201, f"Profile #{i} should succeed for pro tier"


class TestProfileList:
    def test_list_empty_initially(self, client, auth_headers_free):
        r = client.get("/api/v1/profiles/", headers=auth_headers_free)
        assert r.status_code == 200
        assert r.json() == []

    def test_list_returns_created(self, client, auth_headers_pro):
        client.post("/api/v1/profiles/", headers=auth_headers_pro, json={"name": "A"})
        client.post("/api/v1/profiles/", headers=auth_headers_pro, json={"name": "B"})

        r = client.get("/api/v1/profiles/", headers=auth_headers_pro)
        assert r.status_code == 200
        assert len(r.json()) == 2


class TestProfileGet:
    def test_get_by_id(self, client, auth_headers_free):
        created = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={"name": "Lookup"},
        ).json()

        r = client.get(f"/api/v1/profiles/{created['id']}", headers=auth_headers_free)
        assert r.status_code == 200
        assert r.json()["name"] == "Lookup"

    def test_get_missing_returns_404(self, client, auth_headers_free):
        r = client.get("/api/v1/profiles/does-not-exist", headers=auth_headers_free)
        assert r.status_code == 404


class TestProfileUpdate:
    def test_patch_name(self, client, auth_headers_free):
        created = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={"name": "Original"},
        ).json()

        r = client.patch(
            f"/api/v1/profiles/{created['id']}",
            headers=auth_headers_free,
            json={"name": "Renamed"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"

    def test_patch_data(self, client, auth_headers_free):
        created = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={"name": "Data Profile"},
        ).json()

        r = client.patch(
            f"/api/v1/profiles/{created['id']}",
            headers=auth_headers_free,
            json={"data": {"first_name": "Jane"}},
        )
        assert r.status_code == 200

    def test_patch_missing_returns_404(self, client, auth_headers_free):
        r = client.patch(
            "/api/v1/profiles/missing",
            headers=auth_headers_free,
            json={"name": "X"},
        )
        assert r.status_code == 404


class TestProfileDelete:
    def test_delete(self, client, auth_headers_free):
        created = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={"name": "Del me"},
        ).json()

        r = client.delete(f"/api/v1/profiles/{created['id']}", headers=auth_headers_free)
        assert r.status_code == 204

        # Verify gone
        r = client.get(f"/api/v1/profiles/{created['id']}", headers=auth_headers_free)
        assert r.status_code == 404

    def test_delete_missing_returns_404(self, client, auth_headers_free):
        r = client.delete("/api/v1/profiles/missing", headers=auth_headers_free)
        assert r.status_code == 404
