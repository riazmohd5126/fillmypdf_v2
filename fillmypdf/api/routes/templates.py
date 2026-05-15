"""
Template Library API Routes
============================
Endpoints for browsing, filling, and managing the built-in PA form library.

Public (any valid API key):
  GET  /api/v1/templates                      List templates (filterable)
  GET  /api/v1/templates/{id}                 Get full manifest
  GET  /api/v1/templates/{id}/fields          Inspect detected fields (no AI)
  GET  /api/v1/templates/{id}/pdf             Stream the raw PDF
  POST /api/v1/templates/{id}/fill            Fill with one record → PDF
  POST /api/v1/templates/{id}/batch           Fill N records → ZIP

Download (no auth — like batch /download):
  GET  /api/v1/templates/download/{filename}  Retrieve filled PDF or ZIP

Admin only:
  POST   /api/v1/templates                    Upload new template
  PUT    /api/v1/templates/{id}               Update manifest
  DELETE /api/v1/templates/{id}               Remove template
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse

from ...config import settings
from ...models.template import (
    TemplateListResponse,
    TemplateManifest,
    TemplateBatchResponse,
    TemplateFillResponse,
)
from ...services.template_service import TemplateService
from ..dependencies.auth import require_api_key, require_admin
from ..openapi_form_examples import (
    EX_AI_API_KEY,
    EX_AI_BASE_URL,
    EX_AI_MODEL,
    EX_JSON_RECORDS_TWO,
    EX_MANIFEST_JSON_MIN,
    EX_PROFILE_ID,
    EX_USER_DATA_SINGLE,
)

router = APIRouter(
    prefix="/templates",
    tags=["templates"],
    dependencies=[Depends(require_api_key)],
)

_svc = TemplateService()


def _get_service() -> TemplateService:
    """Lazy factory so tests can monkeypatch without a module-level singleton."""
    return TemplateService()


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=TemplateListResponse, summary="List form templates")
async def list_templates(
    category: Optional[str] = Query(
        None,
        description="e.g. prior_authorization",
        examples=["prior_authorization"],
    ),
    drug: Optional[str] = Query(
        None,
        description="Drug name substring, e.g. linzess",
        examples=["linzess"],
    ),
    payer: Optional[str] = Query(
        None,
        description="Payer name substring, e.g. molina",
        examples=["molina"],
    ),
    state: Optional[str] = Query(
        None,
        description="2-letter state, e.g. TX",
        examples=["TX"],
    ),
    specialty: Optional[str] = Query(
        None,
        description="e.g. gi_motility",
        examples=["gi_motility"],
    ),
    tag: Optional[str] = Query(
        None,
        description="Tag match, e.g. medicaid",
        examples=["medicaid"],
    ),
):
    """
    Browse the template library.  All filters are optional and can be combined.

    **Examples:**
    - `GET /api/v1/templates?drug=linzess` — all Linzess forms
    - `GET /api/v1/templates?state=TX&tag=medicaid` — TX Medicaid forms
    - `GET /api/v1/templates?specialty=gi_motility` — GI motility forms
    """
    svc = _get_service()
    items = svc.list(
        category=category,
        drug=drug,
        payer=payer,
        state=state,
        specialty=specialty,
        tag=tag,
    )
    return TemplateListResponse(templates=items, total=len(items))


# ---------------------------------------------------------------------------
# Get manifest
# ---------------------------------------------------------------------------


@router.get("/{template_id}", response_model=TemplateManifest, summary="Get template manifest")
async def get_template(template_id: str):
    """
    Return the full manifest for a template — drug info, payer, indications,
    and the complete questionnaire (key + display text for every Y/N question).

    Use this to render a "fill" UI without downloading the PDF first.
    """
    try:
        return _get_service().get(template_id)
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")


# ---------------------------------------------------------------------------
# Inspect fields (no AI)
# ---------------------------------------------------------------------------


@router.get(
    "/{template_id}/fields",
    summary="Inspect detected form fields (no AI)",
)
async def inspect_template_fields(template_id: str):
    """
    Run CommonForms field detection + pdfplumber label extraction on the stored
    template PDF and return the detected AcroForm fields with inferred labels.

    **No AI call is made.** Use this to validate that field detection looks
    correct before running paid AI fills.
    """
    try:
        svc = _get_service()
        svc.get(template_id)  # ensure exists
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")

    try:
        data = _get_service().inspect_fields(template_id)
    except Exception as exc:
        raise HTTPException(500, f"Field inspection failed: {exc}")

    return data


# ---------------------------------------------------------------------------
# Stream the raw PDF
# ---------------------------------------------------------------------------


@router.get("/{template_id}/pdf", summary="Download the raw template PDF")
async def get_template_pdf(template_id: str):
    """
    Stream the original (static/fillable) template PDF.  Useful for previewing
    the form in a browser or downloading it.
    """
    try:
        pdf_path = _get_service().get_pdf_path(template_id)
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")
    except FileNotFoundError:
        raise HTTPException(404, f"Template '{template_id}' has no PDF on disk")

    return FileResponse(
        path=str(pdf_path),
        filename=f"{template_id}.pdf",
        media_type="application/pdf",
    )


# ---------------------------------------------------------------------------
# Fill — single record
# ---------------------------------------------------------------------------


@router.post(
    "/{template_id}/fill",
    response_model=TemplateFillResponse,
    summary="Fill template with one record",
)
async def fill_template(
    template_id: str,
    background_tasks: BackgroundTasks,
    ai_api_key: str = Form(..., description="AI provider API key", examples=[EX_AI_API_KEY]),
    ai_base_url: str = Form(
        default=EX_AI_BASE_URL,
        description="AI API base URL",
        examples=[EX_AI_BASE_URL],
    ),
    ai_model: str = Form(
        default="gemini-2.5-flash",
        description="AI model",
        examples=[EX_AI_MODEL],
    ),
    user_data: str = Form(
        ...,
        description="JSON object with patient / prescriber / drug data",
        examples=[EX_USER_DATA_SINGLE],
    ),
    profile_id: Optional[str] = Form(
        None,
        description="Saved profile ID to merge",
        examples=[EX_PROFILE_ID],
    ),
    dpi: int = Form(default=200, ge=150, le=300, examples=[200]),
):
    """
    Fill a stored PA template with one patient record.

    The template PDF is converted to a fillable form **once** (cached on disk);
    subsequent fills of the same template skip conversion entirely.  AI field
    mapping results are also cached per template fingerprint.

    **Returns** a JSON payload with `download_url` pointing to the filled PDF.
    Use `GET /api/v1/templates/download/{filename}` to retrieve it.
    """
    try:
        data = json.loads(user_data)
    except json.JSONDecodeError:
        raise HTTPException(400, "user_data must be valid JSON")

    try:
        _get_service().get(template_id)
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")

    try:
        resp = _get_service().fill(
            template_id=template_id,
            user_data=data,
            ai_api_key=ai_api_key,
            ai_base_url=ai_base_url,
            ai_model=ai_model,
            dpi=dpi,
            profile_id=profile_id,
        )
    except Exception as exc:
        raise HTTPException(500, f"Fill failed: {exc}")

    return resp


# ---------------------------------------------------------------------------
# Batch fill
# ---------------------------------------------------------------------------


@router.post(
    "/{template_id}/batch",
    response_model=TemplateBatchResponse,
    summary="Batch-fill template with multiple records",
)
async def batch_fill_template(
    template_id: str,
    background_tasks: BackgroundTasks,
    ai_api_key: str = Form(..., examples=[EX_AI_API_KEY]),
    ai_base_url: str = Form(
        default=EX_AI_BASE_URL,
        examples=[EX_AI_BASE_URL],
    ),
    ai_model: str = Form(
        default="gemini-2.5-flash",
        examples=[EX_AI_MODEL],
    ),
    records: str = Form(
        ...,
        description="JSON array of patient records",
        examples=[EX_JSON_RECORDS_TWO],
    ),
    profile_id: Optional[str] = Form(None, examples=[EX_PROFILE_ID]),
    dpi: int = Form(default=200, ge=150, le=300, examples=[200]),
):
    """
    Fill a stored PA template for many patients at once and return a ZIP.

    The fillable PDF conversion and AI field mapping are both cached, so the
    cost of subsequent batch runs against the same template is minimal.
    """
    try:
        data_list = json.loads(records)
        if not isinstance(data_list, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "records must be a JSON array")

    if not data_list:
        raise HTTPException(400, "records array is empty")
    if len(data_list) > 500:
        raise HTTPException(400, "Maximum 500 records per batch")

    try:
        _get_service().get(template_id)
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")

    try:
        resp = _get_service().fill_batch(
            template_id=template_id,
            records=data_list,
            ai_api_key=ai_api_key,
            ai_base_url=ai_base_url,
            ai_model=ai_model,
            dpi=dpi,
            profile_id=profile_id,
        )
    except Exception as exc:
        raise HTTPException(500, f"Batch fill failed: {exc}")

    return resp


# ---------------------------------------------------------------------------
# Download filled output (no auth required — URL contains unique token)
# ---------------------------------------------------------------------------


@router.get(
    "/download/{filename}",
    summary="Download a filled PDF or batch ZIP",
    dependencies=[],          # override router-level auth
    include_in_schema=True,
)
async def download_filled(filename: str):
    """Download a filled PDF or ZIP produced by /fill or /batch."""
    path = settings.OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(404, "File not found or already cleaned up")
    media = "application/zip" if path.suffix == ".zip" else "application/pdf"
    return FileResponse(
        path=str(path),
        filename=filename,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Admin: upload a new template
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=TemplateManifest,
    status_code=201,
    summary="Upload a new template (admin)",
    dependencies=[Depends(require_admin)],
)
async def upload_template(
    file: UploadFile = File(..., description="Static or fillable PDF"),
    manifest_json: str = Form(
        ...,
        description="TemplateManifest fields as JSON",
        examples=[EX_MANIFEST_JSON_MIN],
    ),
):
    """
    Add a new template to the library.  Requires an **admin** API key.

    The `manifest_json` form field must be a valid JSON object matching the
    `TemplateManifest` schema (minus `created_at` / `updated_at`, which are
    set automatically).  The `id` field must be unique.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "File must be a PDF")

    try:
        raw = json.loads(manifest_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "manifest_json must be valid JSON")

    now = datetime.now(timezone.utc).isoformat()
    raw.setdefault("created_at", now)
    raw["updated_at"] = now

    try:
        manifest = TemplateManifest(**raw)
    except Exception as exc:
        raise HTTPException(422, f"Invalid manifest: {exc}")

    try:
        pdf_bytes = await file.read()
        return _get_service().add(manifest, pdf_bytes)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Could not save template: {exc}")


# ---------------------------------------------------------------------------
# Admin: update manifest
# ---------------------------------------------------------------------------


@router.put(
    "/{template_id}",
    response_model=TemplateManifest,
    summary="Update template manifest (admin)",
    dependencies=[Depends(require_admin)],
)
async def update_template(
    template_id: str,
    manifest_json: str = Form(..., examples=[EX_MANIFEST_JSON_MIN]),
):
    """Update the metadata of an existing template without replacing the PDF."""
    try:
        raw = json.loads(manifest_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "manifest_json must be valid JSON")

    raw["id"] = template_id
    raw["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        manifest = TemplateManifest(**raw)
    except Exception as exc:
        raise HTTPException(422, f"Invalid manifest: {exc}")

    try:
        return _get_service().update_manifest(template_id, manifest)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Update failed: {exc}")


# ---------------------------------------------------------------------------
# Admin: delete
# ---------------------------------------------------------------------------


@router.delete(
    "/{template_id}",
    status_code=204,
    summary="Delete a template (admin)",
    dependencies=[Depends(require_admin)],
)
async def delete_template(template_id: str):
    """Permanently remove a template and its PDF from the library."""
    if not _get_service().delete(template_id):
        raise HTTPException(404, f"Template '{template_id}' not found")
    return None
