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
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse

from ...config import settings
from ...models.template import (
    TemplateListResponse,
    TemplateManifest,
    TemplateBatchResponse,
    TemplateFillResponse,
    SignatureField,
    SignatureFieldsResponse,
    TemplateSignResponse,
)
from ...services.template_service import TemplateService
from ...services.ai_provider import prepare_ai_config
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
    ai_api_key: Optional[str] = Form(
        None,
        description="AI provider API key (required for Gemini; omit when ai_provider='local')",
        examples=[EX_AI_API_KEY],
    ),
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
    ai_provider: Optional[str] = Form(
        None,
        description="'gemini' or 'local' — overrides server AI_PROVIDER for this request",
    ),
    user_data: str = Form(
        ...,
        description="JSON object with patient / prescriber / drug data",
        examples=[EX_USER_DATA_SINGLE],
    ),
    profile_id: Optional[str] = Form(
        None,
        description="Single saved profile ID to merge (legacy)",
        examples=[EX_PROFILE_ID],
    ),
    profile_ids: Optional[str] = Form(
        None,
        description="Comma-separated profile IDs to merge (e.g. 'prof_abc,prof_xyz'). Takes precedence over profile_id.",
    ),
    dpi: int = Form(default=200, ge=150, le=300, examples=[200]),
    return_mappings: bool = Form(
        default=False,
        description="When true, include per-field mappings, confidence scores, and labels in the response (useful for A/B testing)",
    ),
):
    """
    Fill a stored template with one record.

    The template PDF is converted to a fillable form **once** (cached on disk);
    subsequent fills of the same template skip conversion entirely.  AI field
    mapping results are also cached per template fingerprint.

    Pass multiple profiles via `profile_ids` (comma-separated) to merge data
    from e.g. a patient profile and a provider profile before filling.

    **Returns** a JSON payload with `download_url` pointing to the filled PDF.
    Use `GET /api/v1/templates/download/{filename}` to retrieve it.
    """
    try:
        data = json.loads(user_data)
    except json.JSONDecodeError:
        raise HTTPException(400, "user_data must be valid JSON")

    try:
        tpl = _get_service().get(template_id)
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")

    parsed_ids = [p.strip() for p in profile_ids.split(",") if p.strip()] if profile_ids else None

    try:
        resolved_key, resolved_url, resolved_model = prepare_ai_config(
            request_api_key=ai_api_key,
            request_base_url=ai_base_url,
            request_model=ai_model,
            provider_hint=ai_provider,
            category=tpl.category,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    try:
        resp = _get_service().fill(
            template_id=template_id,
            user_data=data,
            ai_api_key=resolved_key,
            ai_base_url=resolved_url,
            ai_model=resolved_model,
            dpi=dpi,
            profile_id=profile_id,
            profile_ids=parsed_ids,
            return_mappings=return_mappings,
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
    ai_api_key: Optional[str] = Form(
        None,
        description="AI provider API key (required for Gemini; omit when ai_provider='local')",
        examples=[EX_AI_API_KEY],
    ),
    ai_base_url: str = Form(
        default=EX_AI_BASE_URL,
        examples=[EX_AI_BASE_URL],
    ),
    ai_model: str = Form(
        default="gemini-2.5-flash",
        examples=[EX_AI_MODEL],
    ),
    ai_provider: Optional[str] = Form(
        None,
        description="'gemini' or 'local' — overrides server AI_PROVIDER for this request",
    ),
    records: str = Form(
        ...,
        description="JSON array of patient records",
        examples=[EX_JSON_RECORDS_TWO],
    ),
    profile_id: Optional[str] = Form(None, examples=[EX_PROFILE_ID]),
    profile_ids: Optional[str] = Form(
        None,
        description="Comma-separated profile IDs to merge. Takes precedence over profile_id.",
    ),
    dpi: int = Form(default=200, ge=150, le=300, examples=[200]),
):
    """
    Fill a stored template for many records at once and return a ZIP.

    The fillable PDF conversion and AI field mapping are both cached, so the
    cost of subsequent batch runs against the same template is minimal.

    Pass multiple profiles via `profile_ids` (comma-separated) to merge base
    data from e.g. an insured profile and an agency profile.
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
        tpl = _get_service().get(template_id)
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")

    parsed_ids = [p.strip() for p in profile_ids.split(",") if p.strip()] if profile_ids else None

    try:
        resolved_key, resolved_url, resolved_model = prepare_ai_config(
            request_api_key=ai_api_key,
            request_base_url=ai_base_url,
            request_model=ai_model,
            provider_hint=ai_provider,
            category=tpl.category,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    try:
        resp = _get_service().fill_batch(
            template_id=template_id,
            records=data_list,
            ai_api_key=resolved_key,
            ai_base_url=resolved_url,
            ai_model=resolved_model,
            dpi=dpi,
            profile_id=profile_id,
            profile_ids=parsed_ids,
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


# ---------------------------------------------------------------------------
# Signature field templates — read
# ---------------------------------------------------------------------------


@router.get(
    "/{template_id}/signature-fields",
    response_model=SignatureFieldsResponse,
    summary="List pre-defined signature zones for a template",
)
async def get_signature_fields(template_id: str):
    """
    Returns the array of named signature zones stored in the template manifest.
    Use the ``key`` value when calling ``POST /templates/{id}/sign`` to avoid
    specifying raw coordinates.
    """
    manifest = _get_service().get(template_id)
    if not manifest:
        raise HTTPException(404, f"Template '{template_id}' not found")
    return SignatureFieldsResponse(
        template_id=template_id,
        signature_fields=manifest.signature_fields,
        total=len(manifest.signature_fields),
    )


# ---------------------------------------------------------------------------
# Admin: update signature fields
# ---------------------------------------------------------------------------


@router.put(
    "/{template_id}/signature-fields",
    response_model=SignatureFieldsResponse,
    summary="Set signature field zones for a template (admin)",
    dependencies=[Depends(require_admin)],
)
async def set_signature_fields(
    template_id: str,
    fields_json: str = Form(
        ...,
        description=(
            'JSON array of signature field objects. '
            'Example: [{"key":"patient_sig","label":"Patient Signature",'
            '"page_index":0,"x_pct":55,"y_pct":5,"width_pct":40,"height_pct":12}]'
        ),
    ),
):
    """
    Replace the entire ``signature_fields`` array on a template manifest.
    Each object must have ``key``, ``label``, ``page_index``, ``x_pct``,
    ``y_pct``, ``width_pct``, ``height_pct``.
    """
    svc = _get_service()
    manifest = svc.get(template_id)
    if not manifest:
        raise HTTPException(404, f"Template '{template_id}' not found")

    try:
        raw_list = json.loads(fields_json)
        if not isinstance(raw_list, list):
            raise ValueError("Must be a JSON array")
        sig_fields = [SignatureField(**item) for item in raw_list]
    except Exception as exc:
        raise HTTPException(422, f"Invalid signature fields: {exc}")

    # Check for duplicate keys
    keys = [f.key for f in sig_fields]
    if len(keys) != len(set(keys)):
        raise HTTPException(422, "Duplicate signature field keys are not allowed")

    manifest.signature_fields = sig_fields
    manifest.updated_at = datetime.now(timezone.utc).isoformat()
    try:
        svc.update_manifest(template_id, manifest)
    except Exception as exc:
        raise HTTPException(500, f"Could not save signature fields: {exc}")

    return SignatureFieldsResponse(
        template_id=template_id,
        signature_fields=sig_fields,
        total=len(sig_fields),
    )


# ---------------------------------------------------------------------------
# Sign a template PDF using a named signature field
# ---------------------------------------------------------------------------


@router.post(
    "/{template_id}/sign",
    response_model=TemplateSignResponse,
    summary="Apply a visual signature to a template PDF using a named signature field",
)
async def sign_template(
    template_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    field_key: str = Form(..., description="Key of the signature field defined in the template manifest"),
    signature_png: Optional[UploadFile] = File(None, description="PNG signature image"),
    signature_text: Optional[str] = Form(None, description="Typed name rendered as cursive PNG"),
    signer_name: Optional[str] = Form(None),
    signer_email: Optional[str] = Form(None),
    consent_given: bool = Form(..., description="Signer must explicitly consent (ESIGN Act)"),
    include_timestamp: bool = Form(True, description="Render 'Signed: YYYY-MM-DD HH:MM UTC' on the signature overlay"),
    pdf_file: Optional[UploadFile] = File(
        None,
        description="Optional filled PDF to sign. If omitted, the raw template PDF is used.",
    ),
):
    """
    Signs a template's PDF at a pre-defined signature zone (looked up by ``field_key``).
    No need to supply raw x/y/width/height coordinates — they come from the template manifest.

    Optionally upload a ``pdf_file`` (e.g. a previously filled output) to sign that instead
    of the raw template.  Returns the signed PDF URL **and** a Certificate of Electronic
    Signature URL.
    """
    from ...services.esign_service import ESignValidationError, apply_signature_overlay, typed_name_to_png
    from ...services.sign_audit_service import SignAuditService
    from ...services.sign_certificate_service import generate_certificate
    from ..dependencies.auth import get_current_key_id
    import uuid as _uuid

    if not consent_given:
        raise HTTPException(400, "consent_given must be true — display the ESIGN disclosure first.")

    manifest = _get_service().get(template_id)
    if not manifest:
        raise HTTPException(404, f"Template '{template_id}' not found")

    # Locate the requested field
    field = next((f for f in manifest.signature_fields if f.key == field_key), None)
    if field is None:
        available = [f.key for f in manifest.signature_fields]
        raise HTTPException(
            404,
            f"Signature field '{field_key}' not found on template '{template_id}'. "
            f"Available: {available or '(none defined)'}",
        )

    has_png = pdf_file is not None and bool(pdf_file.filename)
    has_sig_png = signature_png is not None and bool(signature_png.filename)
    has_sig_text = bool((signature_text or "").strip())

    if has_sig_png and has_sig_text:
        raise HTTPException(400, "Provide either signature_png or signature_text, not both.")
    if not has_sig_png and not has_sig_text:
        raise HTTPException(400, "Provide signature_png or signature_text.")

    # Determine PDF source
    if has_png:
        raw_pdf = await pdf_file.read()
        if len(raw_pdf) > 26_214_400:
            raise HTTPException(400, "PDF exceeds 25 MiB limit")
    else:
        # Use the stored template PDF
        template_pdf_path = settings.STORAGE_DIR / "templates" / template_id / "template.pdf"
        if not template_pdf_path.exists():
            raise HTTPException(404, f"Template PDF not found for '{template_id}'")
        raw_pdf = template_pdf_path.read_bytes()

    # Build signature bytes
    if has_sig_png:
        sig_bytes = await signature_png.read()
        if not sig_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(400, "signature_png must be a valid PNG file")
    else:
        try:
            sig_bytes = typed_name_to_png(signature_text or "")
        except ESignValidationError as e:
            raise HTTPException(400, str(e))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = _uuid.uuid4().hex[:12]
    in_path = settings.UPLOAD_DIR / f"{ts}_{uid}_tmplsign_in.pdf"
    out_name = f"signed_{template_id}_{uid}.pdf"
    out_path = settings.OUTPUT_DIR / out_name

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    in_path.write_bytes(raw_pdf)

    s_name = (signer_name or "").strip() or None
    s_email = (signer_email or "").strip() or None
    client_ip = request.client.host if request.client else None
    audit_id = f"sig_{_uuid.uuid4().hex[:16]}"

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
            page_index=field.page_index,
            x_pct=field.x_pct,
            y_pct=field.y_pct,
            width_pct=field.width_pct,
            height_pct=field.height_pct,
            audit_id=audit_id,
            signer_name=s_name or "",
            signer_email=s_email or "",
            include_timestamp=include_timestamp,
        )
    except ESignValidationError as e:
        background_tasks.add_task(_cleanup)
        raise HTTPException(400, str(e))

    # Generate certificate
    signed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    cert_bytes = generate_certificate(
        audit_id=audit_id,
        document_filename=out_name,
        document_hash=document_hash,
        signer_name=s_name,
        signer_email=s_email,
        signed_at=signed_at,
        client_ip=client_ip,
        page_index=field.page_index,
        signature_mode="draw_or_upload_png" if has_sig_png else "typed",
        placement={"x_pct": field.x_pct, "y_pct": field.y_pct,
                   "width_pct": field.width_pct, "height_pct": field.height_pct},
        api_key_id=get_current_key_id(request),
    )
    cert_path = settings.OUTPUT_DIR / f"certificate_{audit_id}.pdf"
    cert_path.write_bytes(cert_bytes)

    # Record audit
    SignAuditService().record_with_id(
        audit_id=audit_id,
        output_filename=out_name,
        download_url=f"/api/v1/templates/download/{out_name}",
        page_index=field.page_index,
        signature_mode="draw_or_upload_png" if has_sig_png else "typed",
        signer_name=s_name,
        signer_email=s_email,
        api_key_id=get_current_key_id(request),
        client_ip=client_ip,
        placement={"x_pct": field.x_pct, "y_pct": field.y_pct,
                   "width_pct": field.width_pct, "height_pct": field.height_pct},
        document_hash=document_hash,
        consent_given=consent_given,
        certificate_filename=f"certificate_{audit_id}.pdf",
    )

    background_tasks.add_task(_cleanup)

    return TemplateSignResponse(
        template_id=template_id,
        field_key=field_key,
        filename=out_name,
        download_url=f"/api/v1/templates/download/{out_name}",
        certificate_url=f"/api/v1/signatures/certificate/{audit_id}",
        document_hash=document_hash,
        audit_id=audit_id,
    )
