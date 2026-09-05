"""Per-account recipes and my-forms library (pointers + optional private uploads)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from ...models.account import AccountOverview, LibraryResponse, RecipeBody, RecipeRecord
from ...models.template import TemplateManifest
from ...repositories.account_data_repository import AccountDataRepository
from ...services.account_service import account_scope
from ...services.template_service import TemplateService
from ..dependencies.auth import require_api_key

router = APIRouter(
    prefix="/account",
    tags=["account"],
    dependencies=[Depends(require_api_key)],
)


def _data() -> AccountDataRepository:
    return AccountDataRepository()


def _scope(api_key: dict) -> str:
    return account_scope(api_key)


def _my_forms_count(api_key: dict) -> int:
    from ...services.template_access import template_visible

    pins = set(_data().get_library(_scope(api_key)).get("template_ids") or [])
    ids = set(pins)
    for tmpl in TemplateService().list():
        if not template_visible(tmpl, api_key):
            continue
        if tmpl.id in pins or getattr(tmpl, "visibility", "shared") == "private":
            ids.add(tmpl.id)
    return len(ids)


@router.get("/overview", response_model=AccountOverview, summary="Clinic usage overview")
async def account_overview(api_key: dict = Depends(require_api_key)):
    """Counts for the logged-in clinic only — not the shared catalog or global HTTP stats."""
    from ...repositories.job_repository import JobRepository
    from ...services.profile_service import ProfileService
    from ...services.signing_session_service import SigningSessionService

    key_id = api_key.get("id") or ""
    # Force non-admin listing so an admin key still sees this clinic's profiles.
    profiles = ProfileService().list_profiles(owner_id=key_id, tier="pro")
    fills, fills_today = JobRepository().count_for_owner(key_id)
    return AccountOverview(
        fills=fills,
        fills_today=fills_today,
        profiles=len(profiles),
        my_forms=_my_forms_count(api_key),
        sign_sessions=SigningSessionService().count_for_key(key_id),
    )


# ---------------------------------------------------------------------------
# Recipes (form-specific answers, no patient identity)
# ---------------------------------------------------------------------------


@router.get("/recipes", summary="List saved recipes for this clinic")
async def list_recipes(api_key: dict = Depends(require_api_key)):
    rows = _data().list_recipes(_scope(api_key))
    return {"recipes": rows, "total": len(rows)}


@router.get("/recipes/{fingerprint}", response_model=RecipeRecord)
async def get_recipe(fingerprint: str, api_key: dict = Depends(require_api_key)):
    rec = _data().get_recipe(_scope(api_key), fingerprint)
    if not rec:
        raise HTTPException(404, "No saved recipe for this form")
    return RecipeRecord(**rec)


@router.put("/recipes/{fingerprint}", response_model=RecipeRecord)
async def put_recipe(
    fingerprint: str,
    body: RecipeBody,
    api_key: dict = Depends(require_api_key),
):
    if not body.data:
        raise HTTPException(400, "Nothing to save — recipe data is empty")
    rec = _data().save_recipe(
        _scope(api_key),
        fingerprint,
        body.data,
        template_id=body.template_id,
    )
    return RecipeRecord(**rec)


@router.delete("/recipes/{fingerprint}", status_code=204)
async def delete_recipe(fingerprint: str, api_key: dict = Depends(require_api_key)):
    _data().delete_recipe(_scope(api_key), fingerprint)
    return None


# ---------------------------------------------------------------------------
# My forms — pins to the shared catalog (not copies)
# ---------------------------------------------------------------------------


@router.get("/library", response_model=LibraryResponse)
async def get_library(api_key: dict = Depends(require_api_key)):
    rec = _data().get_library(_scope(api_key))
    return LibraryResponse(**rec)


@router.post(
    "/library/uploads",
    response_model=TemplateManifest,
    status_code=201,
    summary="Upload a private clinic form (not copied into the shared catalog)",
)
async def upload_private_template(
    api_key: dict = Depends(require_api_key),
    file: UploadFile = File(..., description="Blank or fillable PDF"),
    name: Optional[str] = Form(None),
    template_id: Optional[str] = Form(None),
    category: str = Form("prior_authorization"),
):
    """Store a form only this clinic can see. Mapping still uses the shared fingerprint cache."""
    filename = file.filename or "form.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "File must be a PDF")
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(400, "PDF is empty")

    raw_id = (template_id or "").strip()
    if not raw_id:
        stem = re.sub(r"[^a-z0-9]+", "-", (name or filename[:-4]).lower()).strip("-")[:40]
        raw_id = f"priv_{stem or 'form'}_{uuid.uuid4().hex[:8]}"
    display = (name or "").strip() or filename[:-4]

    now = datetime.now(timezone.utc).isoformat()
    manifest = TemplateManifest(
        id=raw_id,
        name=display,
        category=category or "general",
        is_public=False,
        visibility="private",
        owner_id=api_key.get("id"),
        org_id=api_key.get("org_id"),
        created_at=now,
        updated_at=now,
    )
    try:
        saved = TemplateService().add(manifest, pdf_bytes)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    _data().pin(_scope(api_key), saved.id)
    return saved


@router.post("/library/{template_id}", response_model=LibraryResponse)
async def pin_template(template_id: str, api_key: dict = Depends(require_api_key)):
    svc = TemplateService()
    try:
        manifest = svc.get(template_id)
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")
    from ...services.template_access import template_visible

    if not template_visible(manifest, api_key):
        raise HTTPException(404, f"Template '{template_id}' not found")
    rec = _data().pin(_scope(api_key), template_id)
    return LibraryResponse(**rec)


@router.delete("/library/{template_id}", response_model=LibraryResponse)
async def unpin_template(template_id: str, api_key: dict = Depends(require_api_key)):
    rec = _data().unpin(_scope(api_key), template_id)
    return LibraryResponse(**rec)
