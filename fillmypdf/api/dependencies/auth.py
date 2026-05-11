"""
Auth Dependencies
=================
FastAPI dependencies for API-key authentication.

Usage:
    @router.get("/secret", dependencies=[Depends(require_api_key)])
    async def secret_endpoint(): ...

    # Or to access the key record itself:
    @router.get("/whoami")
    async def whoami(api_key: dict = Depends(require_api_key)):
        return {"tier": api_key["tier"], "name": api_key["name"]}

    # Admin-only:
    @router.post("/keys", dependencies=[Depends(require_admin)])
    async def create_key(...): ...
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status

from ...services.api_key_service import APIKeyService


def _get_service() -> APIKeyService:
    """Lazy service factory — re-reads settings each call so monkeypatched paths
    (tests) and runtime settings updates are picked up."""
    return APIKeyService()


def _extract_key(x_api_key: Optional[str], authorization: Optional[str]) -> Optional[str]:
    """Accept the key from either X-API-Key header or 'Authorization: Bearer <key>'."""
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
    return None


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """
    Validate the X-API-Key header (or 'Authorization: Bearer ...').
    Attaches the key record to request.state.api_key for downstream handlers.
    Raises 401 on missing key, 403 on invalid/revoked key.
    """
    plain_key = _extract_key(x_api_key, authorization)
    if not plain_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide 'X-API-Key' header or 'Authorization: Bearer <key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    service = _get_service()
    record = service.validate(plain_key)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or revoked API key",
        )

    # Attach to request for downstream consumers (rate limiter, route handlers)
    request.state.api_key = record

    # Best-effort usage tracking (don't block request on failure)
    try:
        service.touch(record["id"])
    except Exception:
        pass

    return record


async def require_admin(
    api_key: dict = Depends(require_api_key),
) -> dict:
    """Require an API key with tier='admin'."""
    if api_key.get("tier") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin API key required for this endpoint",
        )
    return api_key


def get_current_key_id(request: Request) -> Optional[str]:
    """Return the ID of the validated API key from request state, or None."""
    try:
        return request.state.api_key.get("id")
    except AttributeError:
        return None
