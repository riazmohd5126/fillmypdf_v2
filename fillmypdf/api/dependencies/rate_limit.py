"""
Rate Limiting
=============
Tier-based rate limits using slowapi.

Limits (configurable via .env):
  free:     60 req/min,    10_000/day
  pro:      600 req/min,   100_000/day
  business: 6_000 req/min, 1_000_000/day
  admin:    no limit

Bypassed entirely for /health, /docs, /openapi.json, /redoc, and / (root).
"""

from __future__ import annotations

from typing import Optional

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from ...config import settings


def _key_func(request: Request) -> str:
    """
    Use api_key.id as the rate-limit key when authenticated, IP otherwise.
    The auth dependency runs before the rate limit check, so request.state.api_key
    is populated for protected endpoints.
    """
    api_key = getattr(request.state, "api_key", None)
    if api_key:
        return f"key:{api_key['id']}"
    return f"ip:{get_remote_address(request)}"


def _tier_limit(request: Request) -> str:
    """Choose the rate-limit string based on the authenticated tier."""
    api_key = getattr(request.state, "api_key", None)
    tier = (api_key or {}).get("tier", "free")
    return settings.RATE_LIMITS.get(tier, settings.RATE_LIMITS["free"])


# Module-level limiter instance — registered on the FastAPI app in main.py
limiter = Limiter(key_func=_key_func, default_limits=[])


def tier_rate_limit():
    """
    Returns a slowapi limiter decorator that applies the right rate limit
    based on the authenticated tier on each request.
    """
    return limiter.limit(_tier_limit)
