"""Clinic email/password login. API keys remain for Zapier and scripts."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Request, Response

from ...config import settings
from ...models.account import MeResponse, RegisterResponse, UserLogin, UserPublic, UserRegister
from ...services.account_service import AccountService
from ..dependencies.auth import require_api_key

router = APIRouter(prefix="/auth", tags=["auth"])


def _svc() -> AccountService:
    return AccountService()


def _stamp_actor(request: Request, user) -> None:
    """Let activity-audit middleware see who just authenticated."""
    if user is None:
        return
    if hasattr(user, "id"):
        request.state.user = {
            "id": user.id,
            "email": user.email,
            "org_id": user.org_id,
            "role": getattr(user, "role", None),
        }
        request.state.api_key = {
            "id": getattr(user, "api_key_id", None),
            "org_id": user.org_id,
            "tier": getattr(user, "tier", None),
        }
        return
    if isinstance(user, dict):
        request.state.user = user


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(payload: UserRegister, request: Request, response: Response):
    """Create a clinic account, start a session, and issue a linked API key (shown once)."""
    result = _svc().register(payload, response)
    _stamp_actor(request, result.user)
    return result


@router.post("/login", response_model=UserPublic)
async def login(payload: UserLogin, request: Request, response: Response):
    result = _svc().login(payload, response)
    _stamp_actor(request, result)
    return result


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    fmp_session: Optional[str] = Cookie(default=None),
):
    token = fmp_session or request.cookies.get(settings.SESSION_COOKIE_NAME)
    user = _svc().user_from_session(token) if token else None
    _stamp_actor(request, user)
    _svc().logout(token, response)
    return None


@router.get("/me", response_model=MeResponse)
async def me(request: Request, api_key: dict = Depends(require_api_key)):
    """Current clinic user (session) or a synthetic view for API-key-only callers."""
    svc = _svc()
    user = getattr(request.state, "user", None)
    via = getattr(request.state, "auth_via", "api_key")
    if user:
        body = svc.me_from_session(user, api_key)
        return MeResponse(user=body.user, auth=via)
    return svc.me_from_api_key(api_key)
