"""
Card Capture API
=================
POST /api/v1/card-capture/extract — two insurance-card photos (front + back,
already resized/EXIF-stripped client-side) → the 7 canonical insurance
fields with per-field confidence, bounding box, and deterministic
validation (RxBIN format/known-list check, payer fuzzy-match against the
template library).
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from ...config import settings
from ...models.card_capture import CardCaptureResponse
from ...services.ai_provider import assert_egress_allowed, resolve_ai_config
from ...services.card_capture_service import CardCaptureError, CardCaptureService
from ..dependencies.auth import require_api_key
from ..dependencies.rate_limit import ai_tier_rate_limit

router = APIRouter(
    prefix="/card-capture",
    tags=["card-capture"],
    dependencies=[Depends(require_api_key)],
)

_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB — client already resizes to ~1500px


def _guess_mime(upload: UploadFile) -> str:
    ct = (upload.content_type or "").lower()
    if ct.startswith("image/"):
        return ct
    name = (upload.filename or "").lower()
    if name.endswith(".png"):
        return "image/png"
    return "image/jpeg"


@router.post(
    "/extract",
    response_model=CardCaptureResponse,
    summary="Extract the 7 insurance fields from front/back card photos",
)
@ai_tier_rate_limit()
async def extract_card(
    request: Request,
    front: UploadFile = File(..., description="Card front photo"),
    back: UploadFile = File(..., description="Card back photo"),
    ai_provider: str = Form(default=""),
    ai_api_key: str = Form(default=""),
    ai_base_url: str = Form(default=""),
    ai_model: str = Form(default=""),
    api_key: dict = Depends(require_api_key),
):
    front_bytes = await front.read()
    back_bytes = await back.read()
    if not front_bytes or not back_bytes:
        raise HTTPException(400, "Both front and back images are required")
    if len(front_bytes) > _MAX_IMAGE_BYTES or len(back_bytes) > _MAX_IMAGE_BYTES:
        raise HTTPException(400, "Image too large — resize client-side before upload")

    resolved_key = (
        (ai_api_key or "").strip()
        or (settings.GEMINI_API_KEY or "").strip()
        or os.getenv("GEMINI_API_KEY", "")
    )
    provider_hint = (ai_provider or "").strip() or None
    ai_key, ai_base_url_resolved, ai_model_resolved = resolve_ai_config(
        request_api_key=resolved_key,
        request_base_url=(ai_base_url or None),
        request_model=(ai_model or None),
        provider_hint=provider_hint,
    )

    if provider_hint != "local" and settings.AI_PROVIDER != "local" and not ai_key:
        raise HTTPException(
            400,
            "A Gemini API key is required (pass ai_api_key=, set GEMINI_API_KEY, "
            "or switch ai_provider='local' for an on-prem model).",
        )

    try:
        assert_egress_allowed(ai_base_url_resolved)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    service = CardCaptureService(
        api_key=ai_key, base_url=ai_base_url_resolved, model=ai_model_resolved
    )
    try:
        return service.extract(
            front_bytes=front_bytes,
            front_mime=_guess_mime(front),
            back_bytes=back_bytes,
            back_mime=_guess_mime(back),
        )
    except CardCaptureError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # model / network failure
        raise HTTPException(502, f"Card extraction failed: {exc}")
