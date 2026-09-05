"""Clinic login, recipes, and per-account library."""

from __future__ import annotations

import io

from pypdf import PdfWriter


def _pdf_bytes() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class TestRegisterLogin:
    def test_register_sets_cookie_and_returns_api_key(self, client):
        r = client.post(
            "/api/v1/auth/register",
            json={
                "email": "clinic@example.com",
                "password": "password12",
                "clinic_name": "Demo Clinic",
                "name": "Pat",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["user"]["email"] == "clinic@example.com"
        assert body["user"]["org_id"].startswith("org_")
        assert body["api_key"].startswith("fmp_")
        assert client.cookies.get("fmp_session")

    def test_duplicate_email_rejected(self, client):
        payload = {"email": "dup@example.com", "password": "password12"}
        assert client.post("/api/v1/auth/register", json=payload).status_code == 201
        r = client.post("/api/v1/auth/register", json=payload)
        assert r.status_code == 409

    def test_login_and_me(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "a@example.com", "password": "password12"},
        )
        client.post("/api/v1/auth/logout")
        r = client.post(
            "/api/v1/auth/login",
            json={"email": "a@example.com", "password": "password12"},
        )
        assert r.status_code == 200
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["email"] == "a@example.com"
        assert me.json()["auth"] == "session"

    def test_bad_password(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "b@example.com", "password": "password12"},
        )
        r = client.post(
            "/api/v1/auth/login",
            json={"email": "b@example.com", "password": "wrongpass"},
        )
        assert r.status_code == 401

    def test_session_can_list_profiles(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "c@example.com", "password": "password12"},
        )
        r = client.get("/api/v1/profiles/")
        assert r.status_code == 200


class TestAdminEmailLogin:
    def test_seeded_admin_can_login_and_list_keys(self, client, monkeypatch):
        from fillmypdf import config as cfg
        from fillmypdf.services.account_service import AccountService

        monkeypatch.setattr(cfg.settings, "ADMIN_EMAIL", "ops@example.com")
        monkeypatch.setattr(cfg.settings, "ADMIN_PASSWORD", "adminpass12")
        msg = AccountService().ensure_admin_user()
        assert msg and "ops@example.com" in msg

        r = client.post(
            "/api/v1/auth/login",
            json={"email": "ops@example.com", "password": "adminpass12"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tier"] == "admin"
        assert body["role"] == "admin"

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["auth"] == "session"
        assert me.json()["user"]["tier"] == "admin"
        assert me.json()["user"]["role"] == "admin"

        keys = client.get("/api/v1/keys/")
        assert keys.status_code == 200, keys.text

        again = AccountService().ensure_admin_user()
        assert again and "ops@example.com" in again

    def test_clinic_register_stays_pro(self, client):
        r = client.post(
            "/api/v1/auth/register",
            json={"email": "clinic-pro@example.com", "password": "password12"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["user"]["tier"] == "pro"
        assert r.json()["user"]["role"] == "owner"

    def test_cannot_register_reserved_admin_email(self, client, monkeypatch):
        from fillmypdf import config as cfg

        monkeypatch.setattr(cfg.settings, "ADMIN_EMAIL", "ops@example.com")
        r = client.post(
            "/api/v1/auth/register",
            json={"email": "ops@example.com", "password": "password12"},
        )
        assert r.status_code == 409
        assert "reserved" in r.json()["detail"].lower()

    def test_does_not_promote_existing_clinic(self, client, monkeypatch):
        from fillmypdf import config as cfg
        from fillmypdf.services.account_service import AccountService

        client.post(
            "/api/v1/auth/register",
            json={"email": "taken@example.com", "password": "password12"},
        )
        monkeypatch.setattr(cfg.settings, "ADMIN_EMAIL", "taken@example.com")
        monkeypatch.setattr(cfg.settings, "ADMIN_PASSWORD", "adminpass12")
        assert AccountService().ensure_admin_user() is None
        me = client.get("/api/v1/auth/me")
        assert me.json()["user"]["tier"] == "pro"
        assert me.json()["user"]["role"] == "owner"


class TestRecipesAndLibrary:
    def test_recipe_isolated_per_account(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "one@example.com", "password": "password12"},
        )
        fp = "abc123fingerprint"
        put = client.put(
            f"/api/v1/account/recipes/{fp}",
            json={"template_id": "tpl_x", "data": {"clinical.note": "yes"}},
        )
        assert put.status_code == 200, put.text
        got = client.get(f"/api/v1/account/recipes/{fp}")
        assert got.status_code == 200
        assert got.json()["data"]["clinical.note"] == "yes"

        client.post("/api/v1/auth/logout")
        other = client.post(
            "/api/v1/auth/register",
            json={"email": "two@example.com", "password": "password12"},
        )
        assert other.status_code == 201
        missing = client.get(f"/api/v1/account/recipes/{fp}")
        assert missing.status_code == 404

    def test_private_template_hidden_from_other_clinic(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "owner@example.com", "password": "password12"},
        )
        files = {"file": ("form.pdf", _pdf_bytes(), "application/pdf")}
        up = client.post(
            "/api/v1/account/library/uploads",
            files=files,
            data={"name": "My private PA"},
        )
        assert up.status_code == 201, up.text
        tid = up.json()["id"]
        listed = client.get("/api/v1/templates")
        ids = [t["id"] for t in listed.json()["templates"]]
        assert tid in ids

        client.post("/api/v1/auth/logout")
        client.post(
            "/api/v1/auth/register",
            json={"email": "other@example.com", "password": "password12"},
        )
        listed2 = client.get("/api/v1/templates")
        ids2 = [t["id"] for t in listed2.json()["templates"]]
        assert tid not in ids2
        assert client.get(f"/api/v1/templates/{tid}").status_code == 404


class TestAccountOverview:
    def test_overview_is_clinic_scoped(self, client):
        client.post(
            "/api/v1/auth/register",
            json={
                "email": "alpha@example.com",
                "password": "password12",
                "clinic_name": "Alpha Clinic",
            },
        )
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        key_id = me.json()["user"]["api_key_id"]

        prof = client.post(
            "/api/v1/profiles/",
            json={"name": "Patient A", "profile_type": "patient"},
        )
        assert prof.status_code == 201, prof.text

        up = client.post(
            "/api/v1/account/library/uploads",
            files={"file": ("form.pdf", _pdf_bytes(), "application/pdf")},
            data={"name": "Alpha PA"},
        )
        assert up.status_code == 201, up.text

        from fillmypdf.models.job import Job
        from fillmypdf.repositories.job_repository import JobRepository
        from fillmypdf.services.signing_session_service import SigningSessionService

        JobRepository().save(Job(id="job_alpha", record_count=1, api_key_id=key_id))
        JobRepository().save(Job(id="job_other", record_count=1, api_key_id="someone_else"))
        SigningSessionService().create(
            title="Alpha session",
            base_pdf_filename="filled.pdf",
            signers=[{"name": "Dr A", "email": "a@example.com"}],
            created_by_key_id=key_id,
        )
        SigningSessionService().create(
            title="Other session",
            base_pdf_filename="filled.pdf",
            signers=[{"name": "Dr B", "email": "b@example.com"}],
            created_by_key_id="someone_else",
        )

        overview = client.get("/api/v1/account/overview")
        assert overview.status_code == 200, overview.text
        body = overview.json()
        assert body["profiles"] == 1
        assert body["my_forms"] == 1
        assert body["fills"] == 1
        assert body["fills_today"] == 1
        assert body["sign_sessions"] == 1

        client.post("/api/v1/auth/logout")
        client.post(
            "/api/v1/auth/register",
            json={"email": "beta@example.com", "password": "password12"},
        )
        other = client.get("/api/v1/account/overview")
        assert other.status_code == 200, other.text
        empty = other.json()
        assert empty["profiles"] == 0
        assert empty["my_forms"] == 0
        assert empty["fills"] == 0
        assert empty["fills_today"] == 0
        assert empty["sign_sessions"] == 0

    def test_overview_requires_auth(self, client):
        r = client.get("/api/v1/account/overview")
        assert r.status_code in (401, 403)
