"""
Auth Dependencies
=================
FastAPI dependencies for API-key authentication **or** clinic session cookies.

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

from fastapi import Cookie, Depends, Header, HTTPException, Request, status

from ...config import settings
from ...services.account_service import AccountService
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


def _attach(request: Request, record: dict, *, user: Optional[dict] = None, auth: str) -> dict:
    request.state.api_key = record
    request.state.user = user
    request.state.auth_via = auth
    return record


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
    fmp_session: Optional[str] = Cookie(default=None),
) -> dict:
    """
    Validate the X-API-Key header, Bearer token, or clinic session cookie.
    Attaches the key record to request.state.api_key for downstream handlers.
    Raises 401 on missing credentials, 403 on invalid/revoked key.
    """
    cookie_name = settings.SESSION_COOKIE_NAME
    if fmp_session is None:
        fmp_session = request.cookies.get(cookie_name)

    plain_key = _extract_key(x_api_key, authorization)
    service = _get_service()

    if plain_key:
        record = service.validate(plain_key)
        if not record:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or revoked API key",
            )
        request.state.api_key = record
        request.state.user = None
        request.state.auth_via = "api_key"
        try:
            service.touch(record["id"])
        except Exception:
            pass
        return record

    if fmp_session:
        accounts = AccountService()
        user = accounts.user_from_session(fmp_session)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired. Sign in again or provide an API key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        key_id = user.get("api_key_id")
        record = service.repository.get(key_id) if key_id else None
        if not record or record.get("revoked"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account API key is missing or revoked",
            )
        merged = dict(record)
        merged["org_id"] = user.get("org_id") or record.get("org_id")
        merged["user_id"] = user.get("id")
        _attach(request, merged, user=user, auth="session")
        try:
            service.touch(merged["id"])
        except Exception:
            pass
        return merged

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing API key. Provide 'X-API-Key' header or 'Authorization: Bearer <key>', or sign in.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_admin(
    request: Request,
    api_key: dict = Depends(require_api_key),
) -> dict:
    """Require an admin session (linked key tier or user role) or an admin API key."""
    user = getattr(request.state, "user", None) or {}
    if api_key.get("tier") == "admin" or str(user.get("role") or "").lower() == "admin":
        return api_key
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin sign-in required for this endpoint",
    )


def get_current_key_id(request: Request) -> Optional[str]:
    """Return the ID of the validated API key from request state, or None."""
    try:
        return request.state.api_key.get("id")
    except AttributeError:
        return None


def get_current_user(request: Request) -> Optional[dict]:
    try:
        return request.state.user
    except AttributeError:
        return None
