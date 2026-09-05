"""Clinic account, recipe, and my-forms library models."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


def _norm_email(v: str) -> str:
    email = (v or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("Enter a valid email address")
    if len(email) > 200:
        raise ValueError("Email is too long")
    return email


class UserRegister(BaseModel):
    email: str
    password: str = Field(..., min_length=8, max_length=128)
    name: Optional[str] = Field(None, max_length=120)
    clinic_name: Optional[str] = Field(None, max_length=160)

    @field_validator("email")
    @classmethod
    def email_ok(cls, v: str) -> str:
        return _norm_email(v)

    @field_validator("password")
    @classmethod
    def password_ok(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Password cannot be empty")
        return v


class UserLogin(BaseModel):
    email: str
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def email_ok(cls, v: str) -> str:
        return _norm_email(v)


class UserPublic(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    clinic_name: Optional[str] = None
    org_id: str
    role: str = "owner"
    api_key_id: Optional[str] = None
    api_key_prefix: Optional[str] = None
    tier: str = "pro"


class RegisterResponse(BaseModel):
    user: UserPublic
    api_key: str = Field(
        ...,
        description="Plaintext API key for Zapier/scripts. Shown only at registration.",
    )


class MeResponse(BaseModel):
    user: UserPublic
    auth: str = Field(description="'session' or 'api_key'")


class RecipeBody(BaseModel):
    fingerprint: Optional[str] = None
    template_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class RecipeRecord(BaseModel):
    fingerprint: str
    template_id: Optional[str] = None
    saved_at: str
    data: Dict[str, Any] = Field(default_factory=dict)


class LibraryPin(BaseModel):
    template_id: str = Field(..., min_length=1, max_length=120)


class LibraryResponse(BaseModel):
    template_ids: List[str] = Field(default_factory=list)
    updated_at: Optional[str] = None


class AccountOverview(BaseModel):
    """Clinic-scoped dashboard counts (not server-wide usage)."""

    fills: int = 0
    fills_today: int = 0
    profiles: int = 0
    my_forms: int = 0
    sign_sessions: int = 0
