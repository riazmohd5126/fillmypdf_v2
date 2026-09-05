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

import os
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
from ...services.signature_detect_service import SignatureDetectService
from ...services.signature_detect_cache import SignatureDetectCache
from ...services.saved_signature_service import SavedSignatureService
from ...services.ai_provider import prepare_ai_config
from ..dependencies.auth import get_current_key_id, require_admin, require_api_key


router = APIRouter(
    prefix="/signatures",
    tags=["signing"],
    dependencies=[Depends(require_api_key)],
)

MAX_PDF_BYTES = 26_214_400       # 25 MiB
MAX_SIGNATURE_PNG_BYTES = 4_194_304  # 4 MiB

_audit = SignAuditService()
_detect_svc = SignatureDetectService()
_detect_cache = SignatureDetectCache()
_saved_sigs = SavedSignatureService()


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
# POST /signatures/detect-fields  — auto-detect signature placement zones
# ---------------------------------------------------------------------------

@router.post(
    "/detect-fields",
    summary="Auto-detect signature and date fields in a PDF",
)
async def detect_signature_fields(
    file: UploadFile = File(..., description="PDF to analyse"),
    ai_api_key: Optional[str] = Form(
        None,
        description="Optional Gemini key for Strategy 2. Falls back to server GEMINI_API_KEY.",
    ),
    ai_base_url: Optional[str] = Form(None, description="Custom AI base URL (leave blank to use server default)"),
    ai_model: Optional[str] = Form(None, description="AI model name (leave blank to use server default)"),
    ai_provider: Optional[str] = Form(None, description="'gemini' or 'local' — overrides server AI_PROVIDER for this request"),
    max_pages: int = Form(3, ge=1, le=10, description="Max pages to analyse with AI fallback"),
    use_ai: bool = Form(
        True,
        description="If true, run Gemini vision when AcroForm finds no signature zones",
    ),
    force_refresh: bool = Form(
        False,
        description="If true, ignore signature-detect cache and re-run detection",
    ),
):
    """
    Detects signature and date zones in a PDF.

    **Strategy:**
    1. Cache hit (same PDF bytes + options) returns prior zones — no AI.
    2. AcroForm: ``/Sig`` widgets + clear signature-line labels only.
    3. Gemini vision when needed, then printed-label heuristic.

    Successful detections are cached under ``storage/signature_detect_cache/``.
    """
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "File must be a PDF.")

    raw = await file.read()
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(400, f"PDF exceeds {MAX_PDF_BYTES // (1024 * 1024)} MiB limit.")

    try:
        resolved_key, resolved_url, resolved_model = prepare_ai_config(
            request_api_key=ai_api_key,
            request_base_url=ai_base_url,
            request_model=ai_model,
            provider_hint=ai_provider,
            require_cloud_key=False,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    if not (resolved_key or "").strip():
        resolved_key = (settings.GEMINI_API_KEY or "").strip() or os.getenv("GEMINI_API_KEY", "")

    cache_fp = _detect_cache.fingerprint(
        raw, use_ai=use_ai, model=resolved_model or "", max_pages=max_pages
    )
    if not force_refresh:
        cached = _detect_cache.get(cache_fp)
        if cached:
            meta = cached.get("meta") or {}
            fields = cached.get("fields") or []
            msg = cached.get("message") or "Loaded signature zones from cache."
            return {
                "total": len(fields),
                "source": cached.get("source") or "cache",
                "fields": fields,
                "message": f"{msg} (cache hit — skipped AI).",
                "acroform_count": meta.get("acroform_count", 0),
                "acroform_signature_count": meta.get("acroform_signature_count", 0),
                "acroform_date_count": meta.get("acroform_date_count", 0),
                "ai_attempted": False,
                "ai_count": 0,
                "ai_signature_count": meta.get("ai_signature_count", 0),
                "ai_available": bool((resolved_key or "").strip()),
                "ai_skipped_reason": "cache_hit",
                "ai_errors": [],
                "heuristic_used": bool(meta.get("heuristic_used")),
                "heuristic_count": meta.get("heuristic_count", 0),
                "cache_hit": True,
                "cache_fingerprint": cache_fp,
            }

    detected, meta = _detect_svc.detect_with_meta(
        raw,
        ai_api_key=resolved_key or None,
        ai_base_url=resolved_url or None,
        ai_model=resolved_model,
        max_pages_ai=max_pages,
        use_ai=use_ai,
    )

    fields = [
        {
            "key": f.key,
            "label": f.label,
            "page_index": f.page_index,
            "x_pct": f.x_pct,
            "y_pct": f.y_pct,
            "width_pct": f.width_pct,
            "height_pct": f.height_pct,
            "source": f.source,
            "kind": getattr(f, "kind", "signature") or "signature",
            "confidence": f.confidence,
            "description": f.description,
        }
        for f in detected
    ]

    sources = {f["source"] for f in fields}
    kinds = {f["kind"] for f in fields}
    n_sig = sum(1 for f in fields if f["kind"] in ("signature", "initial"))
    if not fields:
        source = "none"
    elif sources == {"acroform"}:
        source = "acroform"
    elif sources == {"ai"}:
        source = "ai"
    elif sources == {"heuristic"}:
        source = "heuristic"
    else:
        source = "mixed"

    skip = meta.get("ai_skipped_reason")
    ai_errors = meta.get("ai_errors") or []
    heuristic_used = bool(meta.get("heuristic_used"))

    if fields and skip == "acroform_signature_found":
        message = (
            f"Detected {n_sig} AcroForm signature zone(s) (Strategy 1). "
            "AI vision was not needed."
        )
    elif fields and any(f["source"] == "ai" for f in fields):
        message = (
            f"No true AcroForm signature widget — Gemini vision (Strategy 2) "
            f"found {n_sig} signature zone(s)"
            + (f" (+ {len(fields) - n_sig} date)" if len(fields) > n_sig else "")
            + "."
        )
    elif fields and heuristic_used:
        err_note = ""
        if skip == "ai_parse_error" and ai_errors:
            err_note = " Gemini replied but JSON was invalid, so "
        elif meta.get("ai_attempted"):
            err_note = " Gemini found none, so "
        elif skip == "no_ai_key":
            err_note = " No AI key, so "
        elif skip == "use_ai_false":
            err_note = " AI disabled, so "
        else:
            err_note = " "
        message = (
            f"No AcroForm signature widget.{err_note}"
            f"used printed-label heuristic ({n_sig} zone(s))."
        )
    elif fields:
        message = f"Detected {len(fields)} zone(s) ({source}; kinds={sorted(kinds)})."
    elif skip == "no_ai_key":
        message = (
            "No AcroForm signature widget (ordinary date fields are ignored). "
            "AI fallback unavailable — set GEMINI_API_KEY or pass ai_api_key. "
            "Printed-label heuristic also found none."
        )
    elif skip == "use_ai_false":
        message = (
            "No AcroForm signature widget. AI fallback was disabled (use_ai=false). "
            "Printed-label heuristic also found none."
        )
    elif skip == "ai_parse_error":
        detail = (ai_errors[0] if ai_errors else "invalid JSON")
        message = (
            "No AcroForm signature widget. Gemini responded but the reply could not be "
            f"parsed ({detail}). Printed-label heuristic also found none — "
            "enter page/coordinates manually."
        )
    elif skip == "ai_found_none":
        message = (
            "No AcroForm signature widget and Gemini vision found none either. "
            "Printed-label heuristic also found none. "
            "Enter page/coordinates manually."
        )
    else:
        message = "No signature zones detected. Enter coordinates manually."

    if fields:
        _detect_cache.save(
            cache_fp,
            fields=fields,
            meta=meta,
            source=source,
            message=message,
        )

    return {
        "total": len(fields),
        "source": source,
        "fields": fields,
        "message": message,
        "acroform_count": meta.get("acroform_count", 0),
        "acroform_signature_count": meta.get("acroform_signature_count", 0),
        "acroform_date_count": meta.get("acroform_date_count", 0),
        "ai_attempted": bool(meta.get("ai_attempted")),
        "ai_count": meta.get("ai_count", 0),
        "ai_signature_count": meta.get("ai_signature_count", 0),
        "ai_available": bool(meta.get("ai_available")),
        "ai_skipped_reason": skip,
        "ai_errors": ai_errors[:5],
        "heuristic_used": heuristic_used,
        "heuristic_count": meta.get("heuristic_count", 0),
        "cache_hit": False,
        "cache_fingerprint": cache_fp,
    }


# ---------------------------------------------------------------------------
# Saved signature (per API key) — reuse without re-drawing
# ---------------------------------------------------------------------------

@router.get("/saved", summary="Get metadata for the caller's saved signature")
async def get_saved_signature_meta(request: Request):
    key_id = get_current_key_id(request)
    if not key_id:
        raise HTTPException(401, "API key required.")
    meta = _saved_sigs.get_meta(key_id)
    if not meta:
        return {"exists": False}
    return meta


@router.get("/saved/png", summary="Download the caller's saved signature PNG")
async def get_saved_signature_png(request: Request):
    key_id = get_current_key_id(request)
    if not key_id:
        raise HTTPException(401, "API key required.")
    png = _saved_sigs.get_png(key_id)
    if not png:
        raise HTTPException(404, "No saved signature for this API key.")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Content-Disposition": 'inline; filename="saved_signature.png"'},
    )


@router.put("/saved", summary="Save or replace the caller's signature PNG")
async def put_saved_signature(
    request: Request,
    signature_png: UploadFile = File(..., description="PNG signature to save for this API key"),
    mode: str = Form("draw"),
    label: str = Form("My signature"),
):
    key_id = get_current_key_id(request)
    if not key_id:
        raise HTTPException(401, "API key required.")
    raw = await signature_png.read()
    if len(raw) > MAX_SIGNATURE_PNG_BYTES:
        raise HTTPException(400, "Signature PNG exceeds 4 MiB.")
    try:
        meta = _saved_sigs.save(key_id, raw, mode=mode, label=label)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **meta}


@router.delete("/saved", summary="Delete the caller's saved signature")
async def delete_saved_signature(request: Request):
    key_id = get_current_key_id(request)
    if not key_id:
        raise HTTPException(401, "API key required.")
    removed = _saved_sigs.delete(key_id)
    return {"ok": True, "deleted": removed}


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
    height_pct: float = Form(4.0, ge=0.1, le=100),
    include_timestamp: bool = Form(True, description="Render a 'Signed: YYYY-MM-DD HH:MM UTC' line at the bottom of the signature box"),
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
                include_timestamp=include_timestamp,
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
