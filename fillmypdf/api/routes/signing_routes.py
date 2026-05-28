"""
Visual e-sign (signature overlay) + legal compliance
=====================================================
Stamps a PNG (drawn or typed) onto a PDF page and produces:
  1. Signed PDF — with SHA-256 hash embedded in PDF metadata.
  2. Certificate of Electronic Signature PDF — human-readable tamper-evident record.
  3. Append-only audit JSONL entry — includes hash, consent flag, IP, and audit ID.

ESIGN / UETA note: requires ``consent_given=true`` — caller must obtain the
signer's affirmative consent before submitting (checkbox in UI, boolean in API).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from ...config import settings
from ...models import SignatureApplyResponse
from ...services.esign_service import ESignValidationError, apply_signature_overlay, typed_name_to_png
from ...services.sign_audit_service import SignAuditService
from ...services.sign_certificate_service import generate_certificate
from ..dependencies.auth import get_current_key_id, require_admin, require_api_key


router = APIRouter(
    prefix="/signatures",
    tags=["signing"],
    dependencies=[Depends(require_api_key)],
)

MAX_PDF_BYTES = 26_214_400       # 25 MiB
MAX_SIGNATURE_PNG_BYTES = 4_194_304  # 4 MiB

_audit = SignAuditService()


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# GET /signatures/audit  — admin only
# ---------------------------------------------------------------------------

@router.get(
    "/audit",
    summary="List recent signature audit events (admin)",
    dependencies=[Depends(require_admin)],
)
async def list_signature_audit(limit: int = 50):
    """
    Workflow audit trail for visual overlays (who/when/output file/hash).
    Includes SHA-256 document hashes and consent flags.
    """
    cap = max(1, min(limit, 500))
    events = _audit.list_recent(limit=cap)
    return {"events": events, "total": len(events)}


# ---------------------------------------------------------------------------
# GET /signatures/certificate/{audit_id}  — regenerate certificate on demand
# ---------------------------------------------------------------------------

@router.get(
    "/certificate/{audit_id}",
    summary="Download the Certificate of Electronic Signature for a signing event",
    response_class=Response,
)
async def get_certificate(audit_id: str):
    """
    Regenerate and return the PDF Certificate of Electronic Signature for a
    previously recorded signing event identified by ``audit_id``.
    """
    entry = _audit.get_by_id(audit_id)
    if not entry:
        raise HTTPException(404, f"Audit entry '{audit_id}' not found.")

    cert_bytes = generate_certificate(
        audit_id=entry["audit_id"],
        document_filename=entry.get("output_filename", "unknown.pdf"),
        document_hash=entry.get("document_sha256") or "(not recorded)",
        signer_name=entry.get("signer_name"),
        signer_email=entry.get("signer_email"),
        signed_at=entry.get("at", ""),
        client_ip=entry.get("client_ip"),
        page_index=entry.get("page_index", 0),
        signature_mode=entry.get("signature_mode", "unknown"),
        placement=entry.get("placement_pct"),
        api_key_id=entry.get("api_key_id"),
    )
    filename = f"certificate_{audit_id}.pdf"
    return Response(
        content=cert_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# POST /signatures/apply  — main signing endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/apply",
    response_model=SignatureApplyResponse,
    summary="Apply visual signature overlay to a PDF (ESIGN/UETA compliant workflow)",
)
async def apply_visual_signature(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF document to sign"),
    signature_png: Optional[UploadFile] = File(None, description="PNG signature image (drawn or uploaded)"),
    signature_text: Optional[str] = Form(None, description="Typed name — rendered as cursive PNG server-side"),
    signer_name: Optional[str] = Form(None, description="Signer's full name (recorded in audit + certificate)"),
    signer_email: Optional[str] = Form(None, description="Signer's email (recorded in audit + certificate)"),
    consent_given: bool = Form(
        ...,
        description=(
            "REQUIRED — signer must affirmatively consent to electronic signing "
            "(ESIGN § 101(c) intent requirement). Set true only after displaying "
            "the disclosure and receiving explicit confirmation."
        ),
    ),
    page_index: int = Form(0, ge=0, description="Zero-based page index"),
    x_pct: float = Form(55.0, ge=0, le=100, description="Left edge of signature box (% of page width)"),
    y_pct: float = Form(5.0, ge=0, le=100, description="Bottom edge of signature box (% of page height)"),
    width_pct: float = Form(40.0, ge=0.1, le=100),
    height_pct: float = Form(12.0, ge=0.1, le=100),
):
    """
    Merges a semi-transparent PNG onto one page inside a rectangle defined by
    percentages of the page MediaBox (origin lower-left).

    **Consent required** — ``consent_given`` must be ``true``; the calling UI is
    responsible for displaying the ESIGN disclosure and capturing consent.

    Returns the signed PDF download URL **and** a Certificate of Electronic
    Signature PDF download URL containing the SHA-256 document hash.
    """
    if not consent_given:
        raise HTTPException(
            400,
            "consent_given must be true. Display the electronic signature disclosure "
            "to the signer and obtain their explicit agreement before submitting.",
        )

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Document must be a .pdf file")

    has_png = signature_png is not None and bool(signature_png.filename)
    has_text = bool((signature_text or "").strip())

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

    # Resolve signer info strings once
    s_name = (signer_name or "").strip() or None
    s_email = (signer_email or "").strip() or None
    client_ip = request.client.host if request.client else None

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

        # Generate a provisional audit ID so we can embed it in PDF metadata
        audit_id = f"sig_{uuid.uuid4().hex[:16]}"

        try:
            document_hash = apply_signature_overlay(
                in_path,
                out_path,
                png_bytes=sig_bytes,
                page_index=page_index,
                x_pct=x_pct,
                y_pct=y_pct,
                width_pct=width_pct,
                height_pct=height_pct,
                audit_id=audit_id,
                signer_name=s_name or "",
                signer_email=s_email or "",
            )
        except ESignValidationError as e:
            raise HTTPException(400, str(e)) from e

        # Generate Certificate of Electronic Signature PDF
        signed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        cert_filename = f"certificate_{audit_id}.pdf"
        cert_path = settings.OUTPUT_DIR / cert_filename
        cert_bytes = generate_certificate(
            audit_id=audit_id,
            document_filename=out_name,
            document_hash=document_hash,
            signer_name=s_name,
            signer_email=s_email,
            signed_at=signed_at,
            client_ip=client_ip,
            page_index=page_index,
            signature_mode=sig_mode,
            placement={"x_pct": x_pct, "y_pct": y_pct, "width_pct": width_pct, "height_pct": height_pct},
            api_key_id=get_current_key_id(request),
        )
        cert_path.write_bytes(cert_bytes)

        certificate_url = f"/api/v1/signatures/certificate/{audit_id}"

        # Record to audit log — uses the pre-generated audit_id
        _audit.record_with_id(
            audit_id=audit_id,
            output_filename=out_name,
            download_url=download_url,
            page_index=page_index,
            signature_mode=sig_mode,
            signer_name=s_name,
            signer_email=s_email,
            api_key_id=get_current_key_id(request),
            client_ip=client_ip,
            placement={"x_pct": x_pct, "y_pct": y_pct, "width_pct": width_pct, "height_pct": height_pct},
            document_hash=document_hash,
            consent_given=consent_given,
            certificate_filename=cert_filename,
        )

        return SignatureApplyResponse(
            filename=out_name,
            download_url=download_url,
            certificate_url=certificate_url,
            document_hash=document_hash,
            page_index=page_index,
            audit_id=audit_id,
        )
    finally:
        background_tasks.add_task(_unlink_if_exists, in_path)
