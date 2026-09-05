"""
Document Approval Workflow Routes
===================================
Mixed-auth endpoints: requester paths require an API key; reviewer paths
are public but token-gated.

  POST   /api/v1/approvals                         Create approval request
  GET    /api/v1/approvals                         List approval requests (requester)
  GET    /api/v1/approvals/output-pdfs             List recent filled PDFs for dropdown
  GET    /api/v1/approvals/{id}                    Get full status (requester)
  GET    /api/v1/approvals/{id}/review?token=..    Public metadata view (reviewer)
  GET    /api/v1/approvals/{id}/download?token=..  Public PDF download (reviewer)
  POST   /api/v1/approvals/{id}/decide?token=..    Approve or reject (reviewer)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ...config import settings
from ...services.approval_service import ApprovalRequest, ApprovalService
from ...services import email_service
from ...services import webhook_emitter
from ..dependencies.auth import get_current_key_id, require_api_key


router = APIRouter(prefix="/approvals", tags=["approvals"])
_svc = ApprovalService()


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _summary(record: ApprovalRequest, *, include_token: bool = False) -> dict:
    d = record.to_dict()
    result = {
        "approval_id": d["approval_id"],
        "title": d["title"],
        "status": d["status"],
        "pdf_filename": d["pdf_filename"],
        "reviewer_name": d.get("reviewer_name", ""),
        "reviewer_email": d.get("reviewer_email", ""),
        "requester_email": d.get("requester_email"),
        "webhook_url": d.get("webhook_url"),
        "expires_at": d["expires_at"],
        "decided_at": d.get("decided_at"),
        "comment": d.get("comment"),
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
    }
    if include_token:
        result["review_url"] = (
            f"/ui/review.html?id={d['approval_id']}&token={d['review_token']}"
        )
    return result


def _public_summary(record: ApprovalRequest) -> dict:
    """Safe summary for the reviewer — no internal IDs beyond what they need."""
    return {
        "approval_id": record.approval_id,
        "title": record.title,
        "status": record.status,
        "reviewer_name": record.reviewer_name,
        "expires_at": record.expires_at,
        "decided_at": record.decided_at,
        "comment": record.comment,
        "download_url": (
            f"/api/v1/approvals/{record.approval_id}/download"
        ),
    }


# ---------------------------------------------------------------------------
# Requester endpoints (API-key required)
# ---------------------------------------------------------------------------

@router.post("", summary="Create a document approval request")
async def create_approval(
    request: Request,
    background_tasks: BackgroundTasks,
    title: str = Form(..., description="Short description of the document"),
    pdf_filename: str = Form(
        ..., description="Filename of the filled PDF in the server output directory"
    ),
    reviewer_name: str = Form("", description="Reviewer's display name"),
    reviewer_email: str = Form("", description="Reviewer's email — receives the review link"),
    requester_email: Optional[str] = Form(
        None, description="Your email — receives the decision notification"
    ),
    webhook_url: Optional[str] = Form(
        None, description="Webhook URL for document.approved / document.rejected events"
    ),
    webhook_secret: Optional[str] = Form(
        None, description="HMAC secret for signing webhook payloads"
    ),
    expires_in_hours: int = Form(
        168, ge=1, le=720, description="Link expiry in hours (default 7 days)"
    ),
    _key: dict = Depends(require_api_key),
):
    """
    Create an approval request for an existing filled PDF.

    The reviewer receives an email with a token link. Anyone with the link
    can approve or reject the document.
    """
    safe_name = Path(pdf_filename or "").name
    if not safe_name or safe_name != pdf_filename.strip() or "/" in pdf_filename or "\\" in pdf_filename:
        raise HTTPException(400, "pdf_filename must be a bare filename (no path).")
    if not safe_name.lower().endswith(".pdf"):
        raise HTTPException(400, "pdf_filename must be a .pdf file.")

    pdf_path = settings.OUTPUT_DIR / safe_name
    if not pdf_path.is_file():
        raise HTTPException(
            404,
            f"PDF '{safe_name}' not found in output directory. "
            "Fill a template first, then pick it from the dropdown.",
        )
    pdf_filename = safe_name

    try:
        record = _svc.create(
            title=title,
            pdf_filename=pdf_filename,
            reviewer_name=reviewer_name,
            reviewer_email=reviewer_email,
            requester_email=requester_email,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            created_by_key_id=get_current_key_id(request),
            expires_in_hours=expires_in_hours,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    if reviewer_email:
        background_tasks.add_task(
            email_service.notify_approval_requested,
            to_email=reviewer_email,
            reviewer_name=reviewer_name or "Reviewer",
            title=title,
            approval_id=record.approval_id,
            review_token=record.review_token,
        )

    return _summary(record, include_token=True)


@router.get("", summary="List approval requests", dependencies=[Depends(require_api_key)])
async def list_approvals(limit: int = 50):
    cap = max(1, min(limit, 200))
    records = _svc.list_all(limit=cap)
    return {"approvals": [_summary(r) for r in records], "total": len(records)}


@router.get(
    "/output-pdfs",
    summary="List recent filled PDFs available for approval",
    dependencies=[Depends(require_api_key)],
)
async def list_output_pdfs(
    limit: int = Query(50, ge=1, le=200, description="Max PDFs to return (newest first)"),
):
    """
    Lists top-level ``.pdf`` files in the server output directory that can be
    sent for approval. Skips certificate PDFs and non-files.
    """
    out = settings.OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    items = []
    try:
        candidates = list(out.glob("*.pdf"))
    except OSError:
        candidates = []

    for path in candidates:
        name = path.name
        if not path.is_file():
            continue
        # Workflow evidence docs — not useful as approval targets
        if name.startswith("certificate_"):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        items.append(
            {
                "filename": name,
                "size_bytes": st.st_size,
                "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )

    items.sort(key=lambda x: x["modified_at"], reverse=True)
    items = items[:limit]
    return {"pdfs": items, "total": len(items)}


@router.get("/{approval_id}", summary="Get approval status", dependencies=[Depends(require_api_key)])
async def get_approval(approval_id: str):
    record = _svc.get(approval_id)
    if not record:
        raise HTTPException(404, f"Approval '{approval_id}' not found.")
    return _summary(record, include_token=True)


# ---------------------------------------------------------------------------
# Public reviewer endpoints (token-gated, no API key)
# ---------------------------------------------------------------------------

@router.get("/{approval_id}/review", summary="Public: view approval metadata")
async def review_approval(approval_id: str, token: str):
    """
    Token-gated public endpoint for the reviewer.
    Returns document metadata and a download link.
    """
    try:
        record = _svc.verify_token_for_read(approval_id, token)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(410, str(exc))  # Gone — expired

    summary = _public_summary(record)
    # Include the token in the download URL so the reviewer can fetch the PDF
    summary["download_url"] = (
        f"/api/v1/approvals/{approval_id}/download?token={token}"
    )
    return summary


@router.get("/{approval_id}/download", summary="Public: download the PDF for review")
async def download_approval_pdf(approval_id: str, token: str):
    """Token-gated PDF download for the reviewer — no API key required."""
    try:
        record = _svc.verify_token_for_read(approval_id, token)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(410, str(exc))

    pdf_path = settings.OUTPUT_DIR / record.pdf_filename
    if not pdf_path.exists():
        raise HTTPException(404, "PDF file not found on server.")

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=record.pdf_filename,
    )


class DecideBody(BaseModel):
    decision: str           # "approved" | "rejected"
    comment: Optional[str] = None


@router.post("/{approval_id}/decide", summary="Public: approve or reject a document")
async def decide_approval(
    approval_id: str,
    token: str,
    body: DecideBody,
    background_tasks: BackgroundTasks,
):
    """
    Anyone with the token link can approve or reject the document.
    Fires a webhook and notifies the requester by email.
    """
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be 'approved' or 'rejected'.")

    try:
        updated = _svc.decide(
            approval_id,
            decision=body.decision,  # type: ignore[arg-type]
            comment=body.comment,
            token=token,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    # Fire webhook in background
    if updated.webhook_url:
        background_tasks.add_task(
            webhook_emitter.fire_event,
            url=updated.webhook_url,
            event=f"document.{body.decision}",
            payload={
                "approval_id": approval_id,
                "title": updated.title,
                "decision": body.decision,
                "comment": body.comment,
                "decided_at": updated.decided_at,
                "pdf_filename": updated.pdf_filename,
            },
            webhook_secret=updated.webhook_secret,
        )

    # Email requester in background
    if updated.requester_email:
        background_tasks.add_task(
            email_service.notify_approval_decided,
            to_email=updated.requester_email,
            title=updated.title,
            decision=body.decision,
            comment=body.comment,
            approval_id=approval_id,
        )

    return _public_summary(updated)
