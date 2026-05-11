"""HMAC-SHA256 signatures for outbound job-completion webhooks."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional


def signature_headers(secret: str, body: bytes, *, unix_ts: Optional[int] = None) -> dict[str, str]:
    """
    Produce ``X-FillMyPDF-Timestamp`` + ``X-FillMyPDF-Signature`` headers.

    Message: ``f\"{unix_ts}.{body}\"`` (exact POST body bytes after the ASCII dot).
    Signature: hex HMAC-SHA256, header ``X-FillMyPDF-Signature: v1=<hex>``.
    """
    ts_str = str(int(time.time()) if unix_ts is None else unix_ts)
    msg = ts_str.encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {"X-FillMyPDF-Timestamp": ts_str, "X-FillMyPDF-Signature": f"v1={digest}"}


def verify(
    *,
    secret: str,
    timestamp_header: str,
    body: bytes,
    signature_header: str,
    max_age_seconds: int = 600,
) -> bool:
    """Verify webhook authenticity (replay window + constant-time MAC compare)."""
    try:
        ts_int = int(timestamp_header)
    except ValueError:
        return False

    now = int(time.time())
    if abs(now - ts_int) > max_age_seconds:
        return False

    raw = signature_header.strip()
    if not raw.startswith("v1="):
        return False
    _, claimed = raw.split("=", 1)
    if not claimed:
        return False

    msg = timestamp_header.encode("utf-8") + b"." + body
    expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(claimed, expected)


def resolve_signing_secret(*, payload_webhook_secret: Optional[str]) -> Optional[str]:
    """Per-job payload secret overrides ``WEBHOOK_SIGNING_SECRET`` (env/settings)."""
    from ..config import settings

    if payload_webhook_secret and str(payload_webhook_secret).strip():
        return str(payload_webhook_secret).strip()
    g = getattr(settings, "WEBHOOK_SIGNING_SECRET", None)
    if g and str(g).strip():
        return str(g).strip()
    return None
