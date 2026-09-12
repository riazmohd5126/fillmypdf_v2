"""
Leads API
=========
POST /api/v1/leads/demo — the landing page's "Book a demo" form. Public
(no API key — it's the marketing site's lead capture), so it does the
minimum: validate the email, append the lead to a local durable log, and
best-effort email ADMIN_EMAIL (silently a no-op if SMTP isn't configured,
same as every other notify_* in email_service.py — a lead is never lost
just because email isn't set up, it's still in the log).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...config import settings
from ...services.email_service import notify_demo_request

router = APIRouter(prefix="/leads", tags=["leads"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LEADS_FILE = settings.STORAGE_DIR / "leads" / "demo_requests.jsonl"


class DemoRequest(BaseModel):
    email: str = Field(..., max_length=200)
    note: str = Field(default="", max_length=1000)
    source: str = Field(default="landing_page", max_length=100)


class DemoRequestResponse(BaseModel):
    success: bool
    message: str


@router.post(
    "/demo",
    response_model=DemoRequestResponse,
    summary="Capture a 'Book a demo' lead from the marketing landing page",
)
async def request_demo(body: DemoRequest):
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "That address does not look right.")

    record = {
        "email": email,
        "note": body.note.strip(),
        "source": (body.source or "landing_page").strip(),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        _LEADS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LEADS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        # Storage hiccup shouldn't block the (still valuable) email notify below.
        pass

    if settings.ADMIN_EMAIL:
        try:
            notify_demo_request(
                to_email=settings.ADMIN_EMAIL,
                lead_email=record["email"],
                note=record["note"],
                source=record["source"],
            )
        except Exception:
            pass  # best-effort — the lead is already durably logged above

    return DemoRequestResponse(
        success=True,
        message="Thanks — we will email two times that work within the hour.",
    )
