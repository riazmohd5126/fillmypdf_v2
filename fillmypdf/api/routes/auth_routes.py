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


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(payload: UserRegister, response: Response):
    """Create a clinic account, start a session, and issue a linked API key (shown once)."""
    return _svc().register(payload, response)


@router.post("/login", response_model=UserPublic)
async def login(payload: UserLogin, response: Response):
    return _svc().login(payload, response)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    fmp_session: Optional[str] = Cookie(default=None),
):
    token = fmp_session or request.cookies.get(settings.SESSION_COOKIE_NAME)
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
