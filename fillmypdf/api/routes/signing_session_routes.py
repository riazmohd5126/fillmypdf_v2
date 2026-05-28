"""
Multi-Party Signing Session Routes
====================================
Endpoints for creating and advancing sequential multi-signer workflows.

  POST   /api/v1/signing-sessions                    Create session
  GET    /api/v1/signing-sessions                    List sessions
  GET    /api/v1/signing-sessions/{id}               Get session detail
  POST   /api/v1/signing-sessions/{id}/sign          Advance — signer N applies signature
  POST   /api/v1/signing-sessions/{id}/cancel        Cancel session
  GET    /api/v1/signing-sessions/{id}/download      Download final signed PDF
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ...config import settings
from ...services.esign_service import ESignValidationError, apply_signature_overlay, typed_name_to_png
from ...services.sign_audit_service import SignAuditService
from ...services.sign_certificate_service import generate_certificate
from ...services.signing_session_service import SigningSessionService
from ..dependencies.auth import get_current_key_id, require_api_key


router = APIRouter(
    prefix="/signing-sessions",
    tags=["signing"],
    dependencies=[Depends(require_api_key)],
)

_sessions = SigningSessionService()
_audit = SignAuditService()


# ── Pydantic shapes ────────────────────────────────────────────────────────

class SignerIn(BaseModel):
    name: str = Field("", description="Signer full name")
    email: str = Field("", description="Signer email address")
    field_key: Optional[str] = Field(None, description="Signature field key from template manifest")
    page_index: int = Field(0, ge=0)
    x_pct: float = Field(55.0, ge=0, le=100)
    y_pct: float = Field(5.0, ge=0, le=100)
    width_pct: float = Field(40.0, ge=0.1, le=100)
    height_pct: float = Field(12.0, ge=0.1, le=100)


class CreateSessionRequest(BaseModel):
    title: str = Field("Untitled Signing Session", min_length=1, max_length=200)
    base_pdf_filename: str = Field(..., description="Filename of the PDF in OUTPUT_DIR to be signed (e.g. filled_abc123.pdf)")
    signers: List[SignerIn] = Field(..., min_length=1, max_length=10)
    template_id: Optional[str] = None


def _session_summary(sess) -> dict:
    d = sess.to_dict()
    current = sess.current_signer()
    return {
        "session_id": d["session_id"],
        "title": d["title"],
        "status": d["status"],
        "template_id": d.get("template_id"),
        "current_signer_index": d["current_signer_index"],
        "total_signers": len(d["signers"]),
        "current_signer": current,
        "signers": d["signers"],
        "base_pdf_filename": d["base_pdf_filename"],
        "current_pdf_filename": d["current_pdf_filename"],
        "final_pdf_filename": d.get("final_pdf_filename"),
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
        "sign_url": f"/ui/multisign.html?session_id={d['session_id']}" if d["status"] != "complete" else None,
        "download_url": f"/api/v1/signing-sessions/{d['session_id']}/download" if d.get("final_pdf_filename") else None,
    }


# ── Create ─────────────────────────────────────────────────────────────────

@router.post("", summary="Create a multi-party signing session")
async def create_session(body: CreateSessionRequest, request: Request):
    """
    Create an ordered signing session. Signers are notified in sequence —
    signer N cannot sign until signer N-1 has signed.

    ``base_pdf_filename`` must be a file that already exists in the server's
    output directory (e.g. a previously filled template PDF).
    """
    pdf_path = settings.OUTPUT_DIR / body.base_pdf_filename
    if not pdf_path.exists():
        # Also check templates storage
        template_pdf = None
        if body.template_id:
            template_pdf = settings.STORAGE_DIR / "templates" / body.template_id / "template.pdf"
        if template_pdf and template_pdf.exists():
            pass  # valid — will copy on first sign
        else:
            raise HTTPException(
                404,
                f"Base PDF '{body.base_pdf_filename}' not found in output directory. "
                "Fill a template first to get a PDF filename."
            )

    try:
        sess = _sessions.create(
            title=body.title,
            base_pdf_filename=body.base_pdf_filename,
            signers=[s.model_dump() for s in body.signers],
            template_id=body.template_id,
            created_by_key_id=get_current_key_id(request),
        )
    except ValueError as e:
        raise HTTPException(422, str(e))

    return _session_summary(sess)


# ── List ───────────────────────────────────────────────────────────────────

@router.get("", summary="List signing sessions")
async def list_sessions(limit: int = 50):
    cap = max(1, min(limit, 200))
    sessions = _sessions.list_all(limit=cap)
    return {
        "sessions": [_session_summary(s) for s in sessions],
        "total": len(sessions),
    }


# ── Get detail ─────────────────────────────────────────────────────────────

@router.get("/{session_id}", summary="Get signing session detail")
async def get_session(session_id: str):
    sess = _sessions.get(session_id)
    if not sess:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    return _session_summary(sess)


# ── Sign (advance session) ─────────────────────────────────────────────────

@router.post("/{session_id}/sign", summary="Apply your signature to advance the session")
async def sign_session_step(
    session_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    signature_png: Optional[UploadFile] = File(None, description="PNG signature image"),
    signature_text: Optional[str] = Form(None, description="Typed name rendered as cursive PNG"),
    signer_name: Optional[str] = Form(None, description="Signer full name (overrides session signer name if provided)"),
    signer_email: Optional[str] = Form(None, description="Signer email"),
    consent_given: bool = Form(..., description="Must be true — ESIGN consent required"),
):
    """
    Apply the current signer's signature to advance the session.
    Uses the placement coordinates stored in the session's signer record.
    When all signers have signed the session status becomes ``complete``.
    """
    if not consent_given:
        raise HTTPException(400, "consent_given must be true.")

    sess = _sessions.get(session_id)
    if not sess:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    if sess.status == "complete":
        raise HTTPException(409, "Session is already complete.")
    if sess.status == "cancelled":
        raise HTTPException(409, "Session has been cancelled.")

    current = sess.current_signer()
    if not current:
        raise HTTPException(409, "No pending signer found.")

    has_sig_png = signature_png is not None and bool(signature_png.filename)
    has_sig_text = bool((signature_text or "").strip())
    if has_sig_png and has_sig_text:
        raise HTTPException(400, "Provide either signature_png or signature_text, not both.")
    if not has_sig_png and not has_sig_text:
        raise HTTPException(400, "Provide signature_png or signature_text.")

    # Build signature bytes
    if has_sig_png:
        sig_bytes = await signature_png.read()
        if not sig_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(400, "signature_png must be a valid PNG file.")
    else:
        try:
            sig_bytes = typed_name_to_png(signature_text or "")
        except ESignValidationError as e:
            raise HTTPException(400, str(e))

    # Resolve current PDF
    current_pdf_path = settings.OUTPUT_DIR / sess.current_pdf_filename
    if not current_pdf_path.exists():
        # Try template raw PDF for first signer
        if sess.template_id:
            tpl_path = settings.STORAGE_DIR / "templates" / sess.template_id / "template.pdf"
            if tpl_path.exists():
                current_pdf_path = tpl_path
        if not current_pdf_path.exists():
            raise HTTPException(404, f"Current PDF '{sess.current_pdf_filename}' not found.")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:12]
    signer_idx = current["index"]
    in_path = settings.UPLOAD_DIR / f"{ts}_{uid}_sess_in.pdf"
    out_name = f"signed_{session_id}_step{signer_idx}_{uid}.pdf"
    out_path = settings.OUTPUT_DIR / out_name

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    in_path.write_bytes(current_pdf_path.read_bytes())

    s_name = (signer_name or current.get("name") or "").strip() or None
    s_email = (signer_email or current.get("email") or "").strip() or None
    client_ip = request.client.host if request.client else None
    audit_id = f"sig_{uuid.uuid4().hex[:16]}"

    def _cleanup():
        try:
            in_path.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        document_hash = apply_signature_overlay(
            in_path,
            out_path,
            png_bytes=sig_bytes,
            page_index=current.get("page_index", 0),
            x_pct=current.get("x_pct", 55.0),
            y_pct=current.get("y_pct", 5.0),
            width_pct=current.get("width_pct", 40.0),
            height_pct=current.get("height_pct", 12.0),
            audit_id=audit_id,
            signer_name=s_name or "",
            signer_email=s_email or "",
        )
    except ESignValidationError as e:
        background_tasks.add_task(_cleanup)
        raise HTTPException(400, str(e))

    # Certificate
    signed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    cert_bytes = generate_certificate(
        audit_id=audit_id,
        document_filename=out_name,
        document_hash=document_hash,
        signer_name=s_name,
        signer_email=s_email,
        signed_at=signed_at,
        client_ip=client_ip,
        page_index=current.get("page_index", 0),
        signature_mode="draw_or_upload_png" if has_sig_png else "typed",
        placement={
            "x_pct": current.get("x_pct", 55.0),
            "y_pct": current.get("y_pct", 5.0),
            "width_pct": current.get("width_pct", 40.0),
            "height_pct": current.get("height_pct", 12.0),
        },
        api_key_id=get_current_key_id(request),
    )
    cert_path = settings.OUTPUT_DIR / f"certificate_{audit_id}.pdf"
    cert_path.write_bytes(cert_bytes)

    # Audit
    _audit.record_with_id(
        audit_id=audit_id,
        output_filename=out_name,
        download_url=f"/api/v1/signing-sessions/{session_id}/download",
        page_index=current.get("page_index", 0),
        signature_mode="draw_or_upload_png" if has_sig_png else "typed",
        signer_name=s_name,
        signer_email=s_email,
        api_key_id=get_current_key_id(request),
        client_ip=client_ip,
        placement={
            "x_pct": current.get("x_pct", 55.0),
            "y_pct": current.get("y_pct", 5.0),
            "width_pct": current.get("width_pct", 40.0),
            "height_pct": current.get("height_pct", 12.0),
        },
        document_hash=document_hash,
        consent_given=consent_given,
        certificate_filename=f"certificate_{audit_id}.pdf",
    )

    # Advance session state
    updated = _sessions.record_signature(
        session_id,
        signer_index=signer_idx,
        signed_pdf_filename=out_name,
        audit_id=audit_id,
    )

    background_tasks.add_task(_cleanup)

    summary = _session_summary(updated)
    summary["step_result"] = {
        "signer_index": signer_idx,
        "signer_name": s_name,
        "filename": out_name,
        "download_url": f"/api/v1/signing-sessions/{session_id}/download",
        "certificate_url": f"/api/v1/signatures/certificate/{audit_id}",
        "document_hash": document_hash,
        "audit_id": audit_id,
    }
    return summary


# ── Cancel ─────────────────────────────────────────────────────────────────

@router.post("/{session_id}/cancel", summary="Cancel a signing session")
async def cancel_session(session_id: str):
    try:
        sess = _sessions.cancel(session_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _session_summary(sess)


# ── Download final PDF ─────────────────────────────────────────────────────

@router.get("/{session_id}/download", summary="Download the fully-signed PDF")
async def download_final_pdf(session_id: str):
    sess = _sessions.get(session_id)
    if not sess:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    if sess.status != "complete":
        raise HTTPException(
            409,
            f"Session is not complete yet (status: {sess.status}). "
            f"Signer {sess.current_signer_index + 1} of {len(sess.signers)} still pending."
        )
    final = settings.OUTPUT_DIR / (sess.final_pdf_filename or "")
    if not final.exists():
        raise HTTPException(404, "Final PDF file not found on server.")
    return FileResponse(
        path=str(final),
        media_type="application/pdf",
        filename=f"fully_signed_{session_id}.pdf",
    )
