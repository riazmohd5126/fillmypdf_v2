"""Outbound webhook delivery for arbitrary FillMyPDF events."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config import settings
from .webhook_signing import resolve_signing_secret, signature_headers


def fire_event(
    *,
    url: str,
    event: str,
    payload: Dict[str, Any],
    webhook_secret: Optional[str] = None,
) -> bool:
    """
    POST a JSON event to ``url`` with optional HMAC signing.

    Returns True if delivery succeeded, False otherwise.
    """
    if not url or not str(url).strip():
        return False

    body = {"event": event, **payload}
    body_bytes = json.dumps(body, default=str).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "X-FillMyPDF-Event": event,
    }

    signing_secret = resolve_signing_secret(payload_webhook_secret=webhook_secret)
    if signing_secret:
        headers.update(signature_headers(signing_secret, body_bytes))

    req = Request(url=str(url).strip(), data=body_bytes, headers=headers, method="POST")

    attempts = max(1, int(getattr(settings, "WEBHOOK_MAX_ATTEMPTS", 4)))
    base_delay = float(getattr(settings, "WEBHOOK_RETRY_BASE_DELAY_SEC", 1.0))

    for attempt in range(attempts):
        try:
            with urlopen(req, timeout=10):
                pass
            return True
        except (URLError, HTTPError, OSError):
            if attempt + 1 >= attempts:
                break
            time.sleep(base_delay * (2**attempt))

    return False
