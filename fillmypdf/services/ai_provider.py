"""
AI Provider Resolver
====================
Central module that resolves which LLM backend to use (Gemini cloud vs.
local Qwen via Ollama/vLLM) and enforces the HIPAA egress guardrail.

All three PHI-touching call sites use this module:
  - VisionService._map_fields_with_ai  (batch fill field mapping)
  - ExtractionService                  (data extraction)
  - SignatureDetectService._detect_ai  (signature zone AI fallback)

Design: ZERO changes to the services themselves.  The resolver runs in the
route layer and hands the resolved (api_key, base_url, model) triple to the
services via their existing constructor / call arguments.

Usage
-----
    from fillmypdf.services.ai_provider import resolve_ai_config, assert_egress_allowed

    api_key, base_url, model = resolve_ai_config(
        request_api_key=form_field_api_key,   # may be None in local mode
        request_base_url=form_field_base_url, # may be None
        request_model=form_field_model,       # may be None
        provider_hint=form_field_provider,    # "gemini" | "local" | None
    )
    assert_egress_allowed(base_url)           # raises ValueError if blocked
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse
from urllib.request import urlopen
from urllib.error import URLError
from typing import Optional

from ..config import settings


# ── RFC-1918 + loopback private ranges ──────────────────────────────────────
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),    # loopback
    ipaddress.ip_network("::1/128"),         # IPv6 loopback
    ipaddress.ip_network("10.0.0.0/8"),      # RFC-1918
    ipaddress.ip_network("172.16.0.0/12"),   # RFC-1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC-1918
    ipaddress.ip_network("fd00::/8"),        # ULA (private IPv6)
]


def _is_private_host(host: str) -> bool:
    """
    Return True only if the host is definitively private/loopback.

    - Known loopback literals → always private.
    - Dotted-decimal / IPv6 → check against RFC-1918 + loopback ranges.
    - Hostnames ending in '.local' → considered private (mDNS / on-prem DNS).
    - Any other hostname (e.g. googleapis.com, api.openai.com) → NOT private.
      We cannot safely resolve arbitrary DNS here, so we deny to protect PHI.
    """
    h = host.lower()
    if h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        addr = ipaddress.ip_address(h)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        # Not a plain IP address.  Only trust .local TLD as a private hostname.
        # All other hostnames are treated as external to protect PHI.
        if h.endswith(".local") or h.endswith(".internal") or h.endswith(".lan"):
            return True
        return False


def assert_egress_allowed(base_url: str) -> None:
    """
    Raise ValueError when AI_LOCAL_ONLY=True and base_url points outside the
    private network.  This is the hard HIPAA guardrail — call it at the route
    layer before passing the URL to any service.

    Does nothing when AI_LOCAL_ONLY=False (default).
    """
    if not settings.AI_LOCAL_ONLY:
        return

    try:
        parsed = urlparse(base_url)
        host = parsed.hostname or ""
    except Exception:
        raise ValueError(
            f"AI_LOCAL_ONLY is enabled but could not parse base_url: {base_url!r}"
        )

    if not _is_private_host(host):
        raise ValueError(
            f"AI_LOCAL_ONLY is enabled — refusing to send data to external host "
            f"'{host}'.  Set AI_LOCAL_ONLY=False or switch AI_PROVIDER to 'local' "
            f"(which points at your Ollama/vLLM server on localhost)."
        )


def resolve_ai_config(
    *,
    request_api_key: Optional[str] = None,
    request_base_url: Optional[str] = None,
    request_model: Optional[str] = None,
    provider_hint: Optional[str] = None,
) -> tuple[str, str, str]:
    """
    Return the resolved (api_key, base_url, model) triple.

    Priority order:
      1. If provider_hint == "local" OR settings.AI_PROVIDER == "local":
            use local Ollama/vLLM settings, ignoring request_* values.
      2. If provider_hint == "gemini" (explicit cloud request):
            use request_* values (falling back to DEFAULT_AI_* settings).
      3. Otherwise (no hint, server default is "gemini"):
            use request_* values, falling back to DEFAULT_AI_* settings.

    This means:
      - In local/HIPAA mode you never need an API key in the request form.
      - In cloud mode the caller must supply their Gemini key (as today).
      - A per-request provider_hint="local" lets a single call opt in even
        when the server default is "gemini".
    """
    effective_provider = (provider_hint or "").strip().lower() or settings.AI_PROVIDER.lower()

    if effective_provider == "local":
        return (
            settings.LOCAL_AI_API_KEY,
            settings.LOCAL_AI_BASE_URL,
            settings.LOCAL_AI_MODEL,
        )

    # Cloud / Gemini path — use request values with server-default fallbacks
    api_key = (request_api_key or "").strip() or ""
    base_url = (request_base_url or "").strip() or settings.DEFAULT_AI_BASE_URL
    model = (request_model or "").strip() or settings.DEFAULT_AI_MODEL
    return api_key, base_url, model


def _local_server_reachable(base_url: str, timeout: float = 1.5) -> bool:
    """
    Probe the local AI server with a short timeout.

    Returns True if the server responds (any HTTP response, even 404/405 —
    we only care that it is running).  Returns False on any network error.

    Used for the fail-open check: when PA_FORCE_LOCAL is on, prefer local
    but fall back to cloud if Ollama/vLLM is not running.
    """
    try:
        # Probe the base URL directly; Ollama returns 200 on "/"
        probe = base_url.rstrip("/").rsplit("/v1", 1)[0] or base_url
        urlopen(probe, timeout=timeout)  # noqa: S310
        return True
    except URLError:
        return False
    except Exception:
        return False


def resolve_provider_for_category(
    category: Optional[str],
    provider_hint: Optional[str],
) -> Optional[str]:
    """
    Decide which provider hint to use based on the template's category.

    Rules (in priority order):
      1. If the caller passed an explicit provider_hint, always honour it.
         (Per-request override always wins — generic and PA callers alike.)
      2. If PA_FORCE_LOCAL is on AND the template category is in PA_CATEGORIES:
           - Probe the local server.
           - Reachable   → return "local"  (PA form goes to Qwen).
           - Unreachable → return None     (fail-open: cloud resolution as usual).
      3. Otherwise return the original provider_hint unchanged.

    This function never raises; failures are silent so callers degrade
    gracefully.  The AI_LOCAL_ONLY hard guardrail in assert_egress_allowed
    will still block cloud egress if needed.
    """
    # Rule 1 — explicit per-request hint always wins
    if provider_hint and provider_hint.strip():
        return provider_hint

    # Rule 2 — PA auto-routing
    if (
        settings.PA_FORCE_LOCAL
        and category
        and category in settings.PA_CATEGORIES
    ):
        if _local_server_reachable(
            settings.LOCAL_AI_BASE_URL,
            timeout=settings.PA_LOCAL_PROBE_TIMEOUT,
        ):
            return "local"
        # Fail-open: local is down, fall through to normal cloud resolution
        print(
            f"  [PA routing] Local server unreachable — falling back to cloud "
            f"for PA template (category={category!r}).  "
            f"Set AI_LOCAL_ONLY=True to block this fallback."
        )
        return None

    # Rule 3 — unchanged
    return provider_hint


def prepare_ai_config(
    *,
    request_api_key: Optional[str] = None,
    request_base_url: Optional[str] = None,
    request_model: Optional[str] = None,
    provider_hint: Optional[str] = None,
    category: Optional[str] = None,
    require_cloud_key: bool = True,
) -> tuple[str, str, str]:
    """
    Resolve provider settings, enforce the HIPAA egress guardrail, and
    validate that a cloud API key is present when required.

    Pass ``category`` (from the template manifest) to activate automatic
    PA-vs-generic routing.  Callers that omit ``category`` (generic/upload
    flows) behave exactly as before.

    Set ``require_cloud_key=False`` for endpoints where AI is optional
    (e.g. signature detect-fields with AcroForm-first fallback).

    Raises ValueError on blocked egress or missing Gemini key in cloud mode.
    """
    # Resolve the effective provider hint, honouring PA auto-routing
    effective_hint = resolve_provider_for_category(category, provider_hint)

    api_key, base_url, model = resolve_ai_config(
        request_api_key=request_api_key,
        request_base_url=request_base_url,
        request_model=request_model,
        provider_hint=effective_hint,
    )
    assert_egress_allowed(base_url)

    effective = (effective_hint or "").strip().lower() or settings.AI_PROVIDER.lower()
    if require_cloud_key and effective != "local" and not (api_key or "").strip():
        raise ValueError(
            "ai_api_key is required when using Gemini/cloud mode. "
            "Set ai_provider=local or configure AI_PROVIDER=local for on-prem Qwen."
        )
    return api_key, base_url, model


def provider_info() -> dict:
    """
    Return a summary dict for the health/usage endpoint so the UI can show
    which provider is currently active and whether local-only mode is on.
    """
    is_local = settings.AI_PROVIDER.lower() == "local"
    return {
        "ai_provider": settings.AI_PROVIDER,
        "ai_local_only": settings.AI_LOCAL_ONLY,
        "ai_use_coordinates": settings.AI_USE_COORDINATES,
        "local_model": settings.LOCAL_AI_MODEL if is_local else None,
        "local_base_url": settings.LOCAL_AI_BASE_URL if is_local else None,
        "cloud_model": settings.DEFAULT_AI_MODEL if not is_local else None,
        "hipaa_mode": is_local and settings.AI_LOCAL_ONLY,
    }
