"""API tests for GET /api/v1/audit (mini app — avoids unrelated route import issues)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fillmypdf.api.middleware.activity_audit import ActivityAuditMiddleware
from fillmypdf.api.routes import audit_routes, auth_routes


@pytest.fixture
def audit_client():
    app = FastAPI()
    app.add_middleware(ActivityAuditMiddleware)
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.include_router(audit_routes.router, prefix="/api/v1")
    with TestClient(app) as c:
        yield c


class TestActivityAuditApi:
    def test_register_appears_in_clinic_audit(self, audit_client):
        r = audit_client.post(
            "/api/v1/auth/register",
            json={
                "email": "audit@example.com",
                "password": "password12",
                "clinic_name": "Audit Clinic",
            },
        )
        assert r.status_code == 201, r.text
        listed = audit_client.get("/api/v1/audit")
        assert listed.status_code == 200, listed.text
        events = listed.json()["events"]
        names = [e["event"] for e in events]
        assert "auth.register" in names
        row = next(e for e in events if e["event"] == "auth.register")
        assert row["actor_email"] == "audit@example.com"
        assert row["org_id"].startswith("org_")
        joined = str(row)
        assert "password12" not in joined

    def test_clinic_cannot_see_other_org(self, audit_client):
        audit_client.post(
            "/api/v1/auth/register",
            json={"email": "one@example.com", "password": "password12"},
        )
        audit_client.post("/api/v1/auth/logout")
        audit_client.post(
            "/api/v1/auth/register",
            json={"email": "two@example.com", "password": "password12"},
        )
        listed = audit_client.get("/api/v1/audit")
        assert listed.status_code == 200
        emails = {e.get("actor_email") for e in listed.json()["events"]}
        assert "two@example.com" in emails
        assert "one@example.com" not in emails

    def test_requires_auth(self, audit_client):
        assert audit_client.get("/api/v1/audit").status_code in (401, 403)
