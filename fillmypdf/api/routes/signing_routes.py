"""
Visual e-sign (signature overlay)
================================
Stamps a PNG (uploaded image or typed name rendered server-side) onto a PDF page.

Uses ``POST …/signatures/apply`` then ``GET /api/v1/batch/download/{filename}`` for retrieval.
This is **not** cryptographic PDF signing (no certificate chain / PAdES).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile

from ...config import settings
from ...models import SignatureApplyResponse
from ...services.esign_service import ESignValidationError, apply_signature_overlay, typed_name_to_png
from ..dependencies.auth import require_api_key


router = APIRouter(
    prefix="/signatures",
    tags=["signing"],
    dependencies=[Depends(require_api_key)],
)

MAX_PDF_BYTES = 26_214_400  # 25 MiB
MAX_SIGNATURE_PNG_BYTES = 4_194_304  # 4 MiB


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@router.post(
    "/apply",
    response_model=SignatureApplyResponse,
    summary="Apply visual signature overlay to a PDF",
)
async def apply_visual_signature(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF document"),
    signature_png: UploadFile | None = File(None, description="PNG with transparency (preferred)"),
    signature_text: str | None = Form(None, description="If no PNG: typed name rendered as PNG"),
    page_index: int = Form(0, ge=0, description="Zero-based page index"),
    x_pct: float = Form(
        55.0,
        ge=0,
        le=100,
        description="Left edge of signature box (% of page width, PDF origin bottom-left)",
    ),
    y_pct: float = Form(
        5.0,
        ge=0,
        le=100,
        description="Bottom edge of signature box (% of page height)",
    ),
    width_pct: float = Form(40.0, ge=0.1, le=100),
    height_pct: float = Form(12.0, ge=0.1, le=100),
):
    """
    Merge a semi-transparent PNG onto **one page** inside a rectangle defined by percentages
    of the page MediaBox (origin lower-left).

    Provide **either** ``signature_png`` **or** ``signature_text``, not both.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Document must be a .pdf file")

    has_png = signature_png is not None and signature_png.filename
    has_text = signature_text is not None and signature_text.strip() != ""

    if has_png and has_text:
        raise HTTPException(400, "Send either signature_png or signature_text, not both")
    if not has_png and not has_text:
        raise HTTPException(400, "Provide signature_png or signature_text")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:12]
    in_path = settings.UPLOAD_DIR / f"{ts}_{uid}_esign_in.pdf"
    out_name = f"signed_{uid}.pdf"
    out_path = settings.OUTPUT_DIR / out_name

    try:
        raw_pdf = await file.read()
        if len(raw_pdf) > MAX_PDF_BYTES:
            raise HTTPException(400, f"PDF exceeds {MAX_PDF_BYTES // (1024 * 1024)} MiB limit")

        if has_png:
            sig_bytes = await signature_png.read()
            if len(sig_bytes) > MAX_SIGNATURE_PNG_BYTES:
                raise HTTPException(400, "Signature PNG exceeds size limit")
            if not sig_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                raise HTTPException(400, "signature_png must be a PNG file")
        else:
            sig_bytes = typed_name_to_png(signature_text or "")

        settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        in_path.write_bytes(raw_pdf)

        try:
            apply_signature_overlay(
                in_path,
                out_path,
                png_bytes=sig_bytes,
                page_index=page_index,
                x_pct=x_pct,
                y_pct=y_pct,
                width_pct=width_pct,
                height_pct=height_pct,
            )
        except ESignValidationError as e:
            raise HTTPException(400, str(e)) from e

        return SignatureApplyResponse(
            filename=out_name,
            download_url=f"/api/v1/batch/download/{out_name}",
            page_index=page_index,
        )
    finally:
        background_tasks.add_task(_unlink_if_exists, in_path)

