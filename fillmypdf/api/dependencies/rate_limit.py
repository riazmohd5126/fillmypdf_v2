"""
Rate Limiting
=============
Tier-based rate limits using slowapi.

Limits (configurable via .env):
  free:     60 req/min,    10_000/day
  pro:      600 req/min,   100_000/day
  business: 6_000 req/min, 1_000_000/day
  admin:    no limit

Bypassed entirely for /health and / (root).

slowapi quirk that shapes this file: a dynamic ``limit_value`` callable
(the thing that picks a limit string per request) is NEVER handed the
``Request`` object. It's called with zero arguments, unless its one
parameter is literally named ``key`` — in which case slowapi calls it with
the *key string* your ``key_func`` derived from the request (see
``LimitGroup.__iter__`` in slowapi/wrappers.py). So per-request state
(here, the caller's tier) has to be smuggled through that key string; a
callable that takes ``request`` directly raises a bare
``TypeError: missing 1 required positional argument`` at request time,
not at import time — easy to ship broken and not notice until it fires.
"""

from __future__ import annotations

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


def _tier_limit(key: str) -> str:
    """Choose the rate-limit string from the tier encoded in the key
    (see _key_func_with_tier) — a bare identity key carries no tier, so
    this only works paired with that key_func, not the plain _key_func."""
    tier = key.rsplit(":", 1)[-1]
    return settings.RATE_LIMITS.get(tier, settings.RATE_LIMITS["free"])


def _key_func_with_tier(request: Request) -> str:
    """Same identity as _key_func, with the tier appended so a dynamic
    limit_value callable (which only ever receives this key string, never
    the Request — see module docstring) can pick a limit by tier."""
    api_key = getattr(request.state, "api_key", None)
    if api_key:
        tier = api_key.get("tier", "free")
        return f"key:{api_key['id']}:{tier}"
    return f"ip:{get_remote_address(request)}:free"


# Module-level limiter instance — registered on the FastAPI app in main.py
limiter = Limiter(key_func=_key_func, default_limits=[])


def tier_rate_limit():
    """
    Returns a slowapi limiter decorator that applies the right rate limit
    based on the authenticated tier on each request.
    """
    return limiter.limit(_tier_limit, key_func=_key_func_with_tier)


def _ai_tier_limit(key: str) -> str:
    """Choose the AI-usage rate-limit string from the tier encoded in the
    key (see _key_func_with_tier). Deliberately tighter than _tier_limit —
    these routes spend real money on the server's own Gemini key per call
    (see AI_RATE_LIMITS)."""
    tier = key.rsplit(":", 1)[-1]
    return settings.AI_RATE_LIMITS.get(tier, settings.AI_RATE_LIMITS["free"])


def ai_tier_rate_limit():
    """
    Returns a slowapi limiter decorator for routes that make a cloud LLM
    call per request (Note Paste, Card Capture) — caps usage per tier
    against AI_RATE_LIMITS instead of the general RATE_LIMITS, so a
    free-tier account can't run up Gemini cost on the server's own key.
    """
    return limiter.limit(_ai_tier_limit, key_func=_key_func_with_tier)
