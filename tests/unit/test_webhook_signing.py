"""Unit tests — webhook signing helpers."""

from unittest.mock import patch

from fillmypdf.config import settings
from fillmypdf.services.webhook_signing import (
    resolve_signing_secret,
    signature_headers,
    verify,
)


def test_signature_roundtrip(monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", None)
    body = b'{"job_id":"j1"}'
    with patch("fillmypdf.services.webhook_signing.time.time", return_value=1_719_998_877.9):
        h = signature_headers("my-secret", body)
    ts = h["X-FillMyPDF-Timestamp"]
    sig = h["X-FillMyPDF-Signature"]
    assert verify(
        secret="my-secret",
        timestamp_header=ts,
        body=body,
        signature_header=sig,
        max_age_seconds=10**12,
    )


def test_verify_rejects_truncated_hmac():
    assert not verify(
        secret="x",
        timestamp_header=str(10**12),
        body=b"a",
        signature_header="v1=beef",
        max_age_seconds=10**12,
    )


def test_resolve_prefers_payload_over_env(monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", "global-secret")
    assert resolve_signing_secret(payload_webhook_secret="  payload  ") == "payload"


def test_resolve_fallback_env(monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", "from-env ")
    assert resolve_signing_secret(payload_webhook_secret=None) == "from-env"


def test_resolve_none_when_blank(monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", None)
    assert resolve_signing_secret(payload_webhook_secret="   ") is None
