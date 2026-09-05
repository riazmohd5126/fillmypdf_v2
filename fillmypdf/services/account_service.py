"""Clinic accounts: register/login plus a linked API key for existing owner scoping."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import HTTPException, Response

from ..config import settings
from ..models import APIKeyCreate
from ..models.account import MeResponse, RegisterResponse, UserLogin, UserPublic, UserRegister
from ..repositories.session_repository import SessionRepository
from ..repositories.user_repository import UserRepository
from .api_key_service import APIKeyService


def account_scope(api_key: dict) -> str:
    """Stable folder id for recipes / my-forms: org if present, else the API key."""
    org = (api_key or {}).get("org_id") or ""
    if org:
        return str(org)
    kid = (api_key or {}).get("id") or "anon"
    return f"key_{kid}"


class AccountService:
    def __init__(self) -> None:
        self.users = UserRepository()
        self.sessions = SessionRepository()
        self.keys = APIKeyService()

    def _public(self, user: dict, key_record: Optional[dict] = None) -> UserPublic:
        rec = key_record
        if rec is None and user.get("api_key_id"):
            rec = self.keys.repository.get(user["api_key_id"])
        return UserPublic(
            id=user["id"],
            email=user["email"],
            name=user.get("name"),
            clinic_name=user.get("clinic_name"),
            org_id=user["org_id"],
            role=user.get("role") or "owner",
            api_key_id=user.get("api_key_id"),
            api_key_prefix=(rec or {}).get("prefix"),
            tier=(rec or {}).get("tier") or "pro",
        )

    def _set_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            key=settings.SESSION_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="lax",
            max_age=int(settings.SESSION_TTL_DAYS) * 86400,
            path="/",
            secure=not settings.DEBUG,
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(
            key=settings.SESSION_COOKIE_NAME,
            path="/",
        )

    @staticmethod
    def reserved_admin_email() -> str:
        return (settings.ADMIN_EMAIL or "").strip().lower()

    def ensure_admin_user(self) -> Optional[str]:
        """Seed the operator account from ADMIN_EMAIL / ADMIN_PASSWORD.

        Creates the user only when missing. Never prints the linked API key.
        Does not silently promote an existing clinic account. Returns a short
        status line for startup logs, or None when skipped.
        """
        email = self.reserved_admin_email()
        password = (settings.ADMIN_PASSWORD or "").strip()
        if not email:
            return None
        if len(password) < 8:
            print(
                "⚠️  ADMIN_EMAIL is set but ADMIN_PASSWORD must be at least "
                "8 characters — skipping admin seed."
            )
            return None

        existing = self.users.get_by_email(email)
        if existing:
            rec = (
                self.keys.repository.get(existing["api_key_id"])
                if existing.get("api_key_id")
                else None
            )
            is_admin = (
                (existing.get("role") or "").lower() == "admin"
                or (rec or {}).get("tier") == "admin"
            )
            if not is_admin:
                print(
                    f"⚠️  ADMIN_EMAIL {email} already exists as a clinic account; "
                    "not promoting. Use a different ADMIN_EMAIL."
                )
                return None
            if (existing.get("role") or "").lower() != "admin":
                existing["role"] = "admin"
                self.users.save(existing)
            if rec and rec.get("tier") != "admin":
                rec["tier"] = "admin"
                self.keys.repository.save(rec)
            return f"Admin operator ready: sign in at /ui/login.html as {email}"

        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        org_id = f"org_{uuid.uuid4().hex[:12]}"
        key_resp = self.keys.create_key(
            APIKeyCreate(
                name="Operator",
                tier="admin",
                owner=email,
            )
        )
        key_rec = self.keys.repository.get(key_resp.id) or {}
        key_rec["org_id"] = org_id
        key_rec["user_id"] = user_id
        self.keys.repository.save(key_rec)

        user = {
            "id": user_id,
            "email": email,
            "name": "Operator",
            "clinic_name": None,
            "org_id": org_id,
            "role": "admin",
            "api_key_id": key_resp.id,
            "password_hash": UserRepository.hash_password(password),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if not self.users.save(user):
            print("⚠️  Could not save seeded admin user")
            return None
        return f"Admin operator ready: sign in at /ui/login.html as {email}"

    def register(self, payload: UserRegister, response: Response) -> RegisterResponse:
        if not settings.AUTH_ALLOW_REGISTER:
            raise HTTPException(403, "Registration is disabled")
        reserved = self.reserved_admin_email()
        if reserved and payload.email == reserved:
            raise HTTPException(409, "This email is reserved for the operator account")
        if self.users.get_by_email(payload.email):
            raise HTTPException(409, "An account with that email already exists")

        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        org_id = f"org_{uuid.uuid4().hex[:12]}"
        name = (payload.name or "").strip() or payload.email.split("@")[0]
        clinic = (payload.clinic_name or "").strip() or None

        key_resp = self.keys.create_key(
            APIKeyCreate(
                name=f"{name} (clinic)",
                tier="pro",
                owner=payload.email,
            )
        )
        key_rec = self.keys.repository.get(key_resp.id) or {}
        key_rec["org_id"] = org_id
        key_rec["user_id"] = user_id
        self.keys.repository.save(key_rec)

        user = {
            "id": user_id,
            "email": payload.email,
            "name": name,
            "clinic_name": clinic,
            "org_id": org_id,
            "role": "owner",
            "api_key_id": key_resp.id,
            "password_hash": UserRepository.hash_password(payload.password),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if not self.users.save(user):
            raise HTTPException(500, "Could not save account")

        token = self.sessions.create(user_id, ttl_days=settings.SESSION_TTL_DAYS)
        self._set_cookie(response, token)
        return RegisterResponse(
            user=self._public(user, key_rec),
            api_key=key_resp.key,
        )

    def login(self, payload: UserLogin, response: Response) -> UserPublic:
        user = self.users.get_by_email(payload.email)
        if not user or not UserRepository.verify_password(
            payload.password, user.get("password_hash") or ""
        ):
            raise HTTPException(401, "Invalid email or password")
        token = self.sessions.create(user["id"], ttl_days=settings.SESSION_TTL_DAYS)
        self._set_cookie(response, token)
        return self._public(user)

    def logout(self, token: Optional[str], response: Response) -> None:
        if token:
            self.sessions.delete(token)
        self.clear_cookie(response)

    def user_from_session(self, token: Optional[str]) -> Optional[dict]:
        sess = self.sessions.get(token or "")
        if not sess:
            return None
        return self.users.get(sess.get("user_id") or "")

    def me_from_api_key(self, api_key: dict) -> Optional[MeResponse]:
        uid = api_key.get("user_id")
        user = self.users.get(uid) if uid else None
        if user is None:
            # API-key-only principal: synthesize a public view so /me still works.
            return MeResponse(
                user=UserPublic(
                    id=api_key.get("id") or "key",
                    email=api_key.get("owner") or "",
                    name=api_key.get("name"),
                    clinic_name=None,
                    org_id=api_key.get("org_id") or f"key_{api_key.get('id')}",
                    role="api",
                    api_key_id=api_key.get("id"),
                    api_key_prefix=api_key.get("prefix"),
                    tier=api_key.get("tier") or "free",
                ),
                auth="api_key",
            )
        return MeResponse(user=self._public(user, api_key), auth="api_key")

    def me_from_session(self, user: dict, api_key: dict) -> MeResponse:
        return MeResponse(user=self._public(user, api_key), auth="session")
