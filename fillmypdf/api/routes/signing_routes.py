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
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile

from ...config import settings
from ...models import SignatureApplyResponse
from ...services.esign_service import ESignValidationError, apply_signature_overlay, typed_name_to_png
from ...services.sign_audit_service import SignAuditService
from ..dependencies.auth import get_current_key_id, require_admin, require_api_key


router = APIRouter(
    prefix="/signatures",
    tags=["signing"],
    dependencies=[Depends(require_api_key)],
)

MAX_PDF_BYTES = 26_214_400  # 25 MiB
MAX_SIGNATURE_PNG_BYTES = 4_194_304  # 4 MiB

_audit = SignAuditService()


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@router.get(
    "/audit",
    summary="List recent signature audit events (admin)",
    dependencies=[Depends(require_admin)],
)
async def list_signature_audit(limit: int = 50):
    """
    Workflow audit trail for visual overlays (who/when/output file).
    **Not** a substitute for ESIGN/UETA legal evidence or PAdES.
    """
    cap = max(1, min(limit, 500))
    events = _audit.list_recent(limit=cap)
    return {"events": events, "total": len(events)}


@router.post(
    "/apply",
    response_model=SignatureApplyResponse,
    summary="Apply visual signature overlay to a PDF",
)
async def apply_visual_signature(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF document"),
    signature_png: UploadFile | None = File(None, description="PNG with transparency (draw or upload)"),
    signature_text: str | None = Form(None, description="If no PNG: typed name rendered as PNG"),
    signer_name: str | None = Form(None, description="Optional display name for audit log"),
    signer_email: str | None = Form(None, description="Optional email for audit log"),
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
    Use the **dashboard** canvas (/dashboard) to draw, or upload a PNG from any tool.
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
    download_url = f"/api/v1/batch/download/{out_name}"
    sig_mode = "draw_or_upload_png" if has_png else "typed"

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

        client_ip = request.client.host if request.client else None
        audit_id = _audit.record(
            output_filename=out_name,
            download_url=download_url,
            page_index=page_index,
            signature_mode=sig_mode,
            signer_name=(signer_name or "").strip() or None,
            signer_email=(signer_email or "").strip() or None,
            api_key_id=get_current_key_id(request),
            client_ip=client_ip,
            placement={"x_pct": x_pct, "y_pct": y_pct, "width_pct": width_pct, "height_pct": height_pct},
        )

        return SignatureApplyResponse(
            filename=out_name,
            download_url=download_url,
            page_index=page_index,
            audit_id=audit_id,
        )
    finally:
        background_tasks.add_task(_unlink_if_exists, in_path)
