"""
Request correlation ID for logs and structured error payloads.
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

_REQUEST_ID_SAFE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def sanitize_incoming_request_id(header_value: Optional[str]) -> Optional[str]:
    """Accept caller-provided IDs only when they match a safe ASCII pattern."""
    if not header_value:
        return None
    v = header_value.strip()
    if not _REQUEST_ID_SAFE.fullmatch(v):
        return None
    return v


def ensure_request_id(request) -> str:
    """Set request.state.request_id if missing and return it."""
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    header_id = sanitize_incoming_request_id(request.headers.get("X-Request-ID"))
    rid = header_id or str(uuid.uuid4())
    request.state.request_id = rid
    return rid
