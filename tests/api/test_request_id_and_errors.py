"""
L2.9 — correlation IDs + structured error payloads
"""

from __future__ import annotations

import pytest


@pytest.fixture
def custom_request_id():
    """Safe ASCII IDs only (see sanitize_incoming_request_id)."""
    return "req-test-9012abcd"


class TestCorrelationHeadersSuccess:
    def test_health_echoes_generated_request_id_header(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert "x-request-id" in {k.lower() for k in r.headers.keys()}
        rid = r.headers.get("x-request-id") or r.headers.get("X-Request-ID")
        assert rid and len(rid) >= 8

    def test_health_respects_client_x_request_id(self, client, custom_request_id):
        r = client.get("/health", headers={"X-Request-ID": custom_request_id})
        assert r.headers.get("X-Request-ID") == custom_request_id

    def test_unsafe_request_id_header_is_replaced(self, client):
        malicious = "../../../etc/passwd"
        r = client.get("/health", headers={"X-Request-ID": malicious})
        assert r.headers.get("X-Request-ID") != malicious
        uuid_like = r.headers.get("X-Request-ID") or ""
        assert len(uuid_like) >= 36  # freshly generated UUID string


class TestStructuredErrors:
    def test_missing_api_key_returns_request_id_body(self, client):
        r = client.get("/api/v1/profiles/")
        assert r.status_code == 401
        data = r.json()
        assert "API key" in data["detail"]
        assert isinstance(data["request_id"], str) and len(data["request_id"]) >= 8
        hdr = r.headers.get("X-Request-ID")
        assert hdr == data["request_id"]

    def test_validation_error_includes_request_id(self, client, auth_headers_free):
        r = client.post(
            "/api/v1/profiles/",
            headers=auth_headers_free,
            json={"name": "", "profile_type": "personal"},
        )
        assert r.status_code == 422
        data = r.json()
        assert "detail" in data
        assert "request_id" in data
        assert r.headers.get("X-Request-ID") == data["request_id"]

    def test_not_found_includes_request_id(self, client, auth_headers_free):
        r = client.get("/api/v1/profiles/does-not-exist-999", headers=auth_headers_free)
        assert r.status_code == 404
        data = r.json()
        assert "request_id" in data
        assert r.headers.get("X-Request-ID") == data["request_id"]


class TestRequestContextHelpers:
    def test_sanitize_rejects_invalid(self):
        from fillmypdf.api.request_context import sanitize_incoming_request_id

        assert sanitize_incoming_request_id(None) is None
        assert sanitize_incoming_request_id("") is None
        assert sanitize_incoming_request_id("short") is None
        assert sanitize_incoming_request_id("a" * 200) is None
        assert sanitize_incoming_request_id("no spaces please") is None

    def test_sanitize_accepts_safe(self):
        from fillmypdf.api.request_context import sanitize_incoming_request_id

        assert sanitize_incoming_request_id("req-12345678") == "req-12345678"
        assert sanitize_incoming_request_id(" ABCdef12-_ ") == "ABCdef12-_"
