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


def prepare_ai_config(
    *,
    request_api_key: Optional[str] = None,
    request_base_url: Optional[str] = None,
    request_model: Optional[str] = None,
    provider_hint: Optional[str] = None,
    require_cloud_key: bool = True,
) -> tuple[str, str, str]:
    """
    Resolve provider settings, enforce the HIPAA egress guardrail, and
    validate that a cloud API key is present when required.

    Set ``require_cloud_key=False`` for endpoints where AI is optional
    (e.g. signature detect-fields with AcroForm-first fallback).

    Raises ValueError on blocked egress or missing Gemini key in cloud mode.
    """
    api_key, base_url, model = resolve_ai_config(
        request_api_key=request_api_key,
        request_base_url=request_base_url,
        request_model=request_model,
        provider_hint=provider_hint,
    )
    assert_egress_allowed(base_url)

    effective = (provider_hint or "").strip().lower() or settings.AI_PROVIDER.lower()
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
