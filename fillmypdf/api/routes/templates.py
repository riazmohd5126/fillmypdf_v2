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
    TemplateReadinessItem,
    TemplateReadinessResponse,
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
# Guided-fill readiness (must be registered before /{template_id})
# ---------------------------------------------------------------------------


def _norm_map_key(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


@router.get(
    "/readiness",
    response_model=TemplateReadinessResponse,
    summary="Which templates have a locked map for Guided Fill",
)
async def templates_readiness():
    """Return Ready / Needs mapping status for every template.

    Matches templates to canonical maps by structure signature when cheap, and
    falls back to normalizing ``form_label`` ↔ template id/name. Used by the
    Template Library and Guided Fill pickers.
    """
    from ...services.canonical_map_cache import CanonicalMapCache
    from ...services.vision_service import VisionService

    svc = _get_service()
    items = svc.list()
    cache = CanonicalMapCache()
    entries = cache.list_entries()

    by_sig: dict = {}
    by_label: dict = {}
    for e in entries:
        sig = (e.get("signature") or "").strip()
        fp = e.get("fingerprint")
        label = e.get("form_label") or ""
        ready = bool(e.get("reviewed"))
        row = {
            "ready": ready,
            "fingerprint": fp,
            "signature": sig or None,
            "form_label": label or None,
        }
        if sig:
            # Prefer a reviewed entry when multiple exist for one signature.
            prev = by_sig.get(sig)
            if prev is None or (ready and not prev.get("ready")):
                by_sig[sig] = row
        nk = _norm_map_key(label)
        if nk:
            prev = by_label.get(nk)
            if prev is None or (ready and not prev.get("ready")):
                by_label[nk] = row

    vs = VisionService("", "", "")
    out: list[TemplateReadinessItem] = []
    for t in items:
        hit = by_label.get(_norm_map_key(t.id)) or by_label.get(_norm_map_key(t.name))
        if hit is None:
            # Compute structure signature for a precise match (fillable cache helps).
            try:
                fillable = svc._ensure_fillable(t.id)
                fields = vs._get_fields_with_coords(str(fillable))
                if fields:
                    sig = cache.signature(fields)
                    hit = by_sig.get(sig) or {
                        "ready": False,
                        "fingerprint": None,
                        "signature": sig,
                        "form_label": None,
                    }
            except Exception:
                hit = None
        if hit is None:
            hit = {
                "ready": False,
                "fingerprint": None,
                "signature": None,
                "form_label": None,
            }
        out.append(
            TemplateReadinessItem(
                template_id=t.id,
                ready=bool(hit.get("ready")),
                fingerprint=hit.get("fingerprint"),
                signature=hit.get("signature"),
                form_label=hit.get("form_label"),
            )
        )

    ready_count = sum(1 for r in out if r.ready)
    return TemplateReadinessResponse(items=out, ready_count=ready_count, total=len(out))


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
# Intake schema (guided web form / CSV) from the locked canonical map
# ---------------------------------------------------------------------------


@router.get(
    "/{template_id}/schema",
    summary="Canonical intake schema for a template (from its reviewed/locked map)",
)
async def get_template_schema(template_id: str):
    """
    Return the set of canonical fields this template needs, derived from its
    **locked** canonical map (see the Mapping Review workflow). Drives the guided
    web form and the CSV template — no AI, PHI-free.

    If the form has no reviewed/locked map yet, ``reviewed`` is ``false`` and
    ``schema`` is empty — review + lock the mapping first for a trustworthy form.
    """
    from ...services.canonical_map_cache import CanonicalMapCache
    from ...services.canonical_schema import derive_schema, intake_schema
    from ...services.form_spec_cache import FormSpecCache
    from ...services.vision_service import VisionService

    try:
        svc = _get_service()
        svc.get(template_id)  # ensure exists
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")

    try:
        fillable_path = svc._ensure_fillable(template_id)
        fields_info = VisionService("", "", "")._get_fields_with_coords(str(fillable_path))
    except Exception as exc:
        raise HTTPException(500, f"Field inspection failed: {exc}")

    if not fields_info:
        return {"template_id": template_id, "reviewed": False,
                "message": "No fillable fields detected",
                "schema": {"fields": [], "groups": []},
                "intake": {"canonical": {"fields": [], "groups": []},
                           "questions": [], "tables": [], "narratives": [],
                           "extras": [], "signatures": [], "rules": {}}}

    cache = CanonicalMapCache()
    sig = cache.signature(fields_info)
    locked = cache.get_by_signature(sig)

    if locked is None:
        draft = cache.find_by_signature(sig) or {}
        return {
            "template_id": template_id,
            "reviewed": False,
            "signature": sig,
            "fingerprint": draft.get("fingerprint"),
            "message": "No reviewed/locked mapping for this form yet. "
                       "Review and lock it in Mapping Review first.",
            "schema": {"fields": [], "groups": []},
            "intake": {"canonical": {"fields": [], "groups": []},
                       "questions": [], "tables": [], "narratives": [],
                       "extras": [], "signatures": [], "rules": {}},
        }

    mappings = locked.get("mappings", {})
    form_spec = FormSpecCache().get(sig)

    # Keep branches form-specific: prune choice rows from older canonical caches,
    # and (re)attach unlock rules when the FormSpec still lacks them.
    from ...services.field_classifier import prune_form_specific_mappings
    from ...services.intake_rules import apply_intake_annotations, sync_field_kinds

    cleaned_preview, n_drop = prune_form_specific_mappings(mappings, fields_info)
    has_q_rule = bool(form_spec) and any(getattr(q, "rule", None) for q in form_spec.questions)
    has_skip = bool(form_spec) and any(
        o.skip_logic for q in form_spec.questions for o in q.options
    )
    from ...services.form_spec_refresh import (
        needs_signatures_rebuild,
        rebuild_form_spec_for_signatures,
    )

    needs_spec_rebuild = needs_signatures_rebuild(form_spec)
    needs_refresh = form_spec is not None and (
        needs_spec_rebuild
        or n_drop > 0
        or (has_skip and not has_q_rule)
        or not getattr(form_spec, "tables", None)
        or int(getattr(form_spec, "extras_version", 0) or 0) < 1
    )
    if needs_refresh:
        try:
            from ...config import settings as _settings

            vs = VisionService(
                (_settings.GEMINI_API_KEY or "").strip() or "-",
                _settings.DEFAULT_AI_BASE_URL,
                _settings.DEFAULT_AI_MODEL,
            )
            if needs_spec_rebuild:
                rebuilt = rebuild_form_spec_for_signatures(
                    sig,
                    form_label=form_spec.form_label or template_id,
                    fillable_path=str(fillable_path),
                    fields_info=fields_info,
                    entry=locked,
                    widget_key=vs._widget_key,
                )
                if rebuilt is not None:
                    form_spec = rebuilt
                    locked = cache.get_by_signature(sig) or locked
                    mappings = locked.get("mappings", mappings)
                    print(f"  🔄  Rebuilt FormSpec for {template_id} "
                          f"(signatures_version bump)")
            label_data = vs.rich_label_data(
                str(fillable_path), fields_info, allow_ai=False
            )
            mappings, form_spec = apply_intake_annotations(
                cleaned_preview if n_drop else mappings,
                fields_info, label_data, form_spec,
                widget_key=vs._widget_key,
            )
            locked = dict(locked)
            locked["mappings"] = mappings
            locked = sync_field_kinds(locked, fields_info)
            cache.save_full(locked.get("fingerprint") or sig, locked)
            FormSpecCache().save(form_spec)
        except Exception as exc:
            print(f"  ⚠️  intake rule annotate skipped: {exc}")
            mappings = cleaned_preview
    elif n_drop > 0:
        mappings = cleaned_preview
        locked = sync_field_kinds(dict(locked), fields_info)
        locked["mappings"] = mappings
        cache.save_full(locked.get("fingerprint") or sig, locked)

    schema = derive_schema(mappings)
    intake = intake_schema(mappings, form_spec)

    # Annotate fields that map to a multi-row table column so the guided form can
    # offer per-row inputs (and list values distribute one value per row).
    from collections import defaultdict
    by_path: dict = defaultdict(list)
    for name, m in mappings.items():
        if isinstance(m, dict) and m.get("canonical") and m["canonical"] != "other":
            by_path[m["canonical"]].append(name)
    rows_by_path = {
        p: len(VisionService.largest_row_run(names, fields_info))
        for p, names in by_path.items()
    }

    def _annotate(field: dict) -> dict:
        field["rows"] = rows_by_path.get(field.get("canonical"), 1)
        return field

    for f in schema.get("fields", []):
        _annotate(f)
    for g in schema.get("groups", []):
        for f in g.get("fields", []):
            _annotate(f)
    for f in intake.get("canonical", {}).get("fields", []):
        _annotate(f)
    for g in intake.get("canonical", {}).get("groups", []):
        for f in g.get("fields", []):
            _annotate(f)

    # Attach e-sign box placement (%) so Guided Fill can stamp signatures
    # after fill without asking the user for coordinates.
    try:
        from ...services.esign_service import enrich_signature_placements

        intake["signatures"] = enrich_signature_placements(
            intake.get("signatures") or [],
            fields_info,
            fillable_path,
        )
    except Exception as exc:
        print(f"  ⚠️  signature placement enrich skipped: {exc}")

    return {
        "template_id": template_id,
        "reviewed": True,
        "fingerprint": locked.get("fingerprint"),
        "signature": sig,
        "schema": schema,
        "intake": intake,
    }


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
    api_key: dict = Depends(require_api_key),
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
    guided: bool = Form(
        default=False,
        description="When true, user_data is already keyed by canonical path "
        "(patient.dob) and may contain LIST values for repeating table rows "
        "(drug history, labs). Bypasses InputAdapter for deterministic fill.",
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
            preserve_input=guided,
            owner_id=api_key.get("id"),
            tier=api_key.get("tier", "free"),
        )
    except Exception as exc:
        raise HTTPException(500, f"Fill failed: {exc}")

    return resp


# ---------------------------------------------------------------------------
# Guided Batch — locked template + intake CSV
# ---------------------------------------------------------------------------


def _intake_for_template(template_id: str) -> tuple[dict, str, str]:
    """Return (intake_schema, fingerprint, signature) for a locked template."""
    from ...services.canonical_map_cache import CanonicalMapCache
    from ...services.canonical_schema import intake_schema
    from ...services.form_spec_cache import FormSpecCache
    from ...services.vision_service import VisionService

    svc = _get_service()
    try:
        svc.get(template_id)
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")
    fillable = svc._ensure_fillable(template_id)
    fields = VisionService("", "", "")._get_fields_with_coords(str(fillable))
    if not fields:
        raise HTTPException(400, "No fillable fields on this template")
    cache = CanonicalMapCache()
    sig = cache.signature(fields)
    locked = cache.get_by_signature(sig)
    if locked is None:
        raise HTTPException(
            409,
            "No locked mapping for this template. Lock it in Mapping Review first.",
        )
    spec = FormSpecCache().get(sig)
    intake = intake_schema(locked.get("mappings") or {}, spec)
    return intake, locked.get("fingerprint") or "", sig


@router.get(
    "/{template_id}/guided-csv",
    summary="Download Guided Batch CSV template + column legend",
)
async def download_guided_csv_template(template_id: str):
    """CSV header row for Guided Batch, plus JSON legend in the response body.

    Returns JSON: ``{csv, headers, legend, fingerprint, signature}``.
    Use the ``csv`` string as the first line of your spreadsheet (or download
    as text/csv via Accept header).
    """
    from ...services.canonical_schema import intake_csv_headers, intake_csv_legend
    import csv as _csv
    import io as _io

    intake, fp, sig = _intake_for_template(template_id)
    headers = intake_csv_headers(intake)
    legend = intake_csv_legend(intake)
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(headers)
    return {
        "template_id": template_id,
        "fingerprint": fp,
        "signature": sig,
        "headers": headers,
        "legend": legend,
        "csv": buf.getvalue(),
        "notes": {
            "canonical": "Catalog paths like patient.dob — prefill from profiles",
            "q:": "Form-specific question answers (use option labels or export values)",
            "t:": "Direct AcroForm writes: narratives, table cells, typed signatures",
            "signature_mode": "Use t:<signature_field> columns with signature_mode=typed",
        },
    }


@router.post(
    "/{template_id}/batch-csv",
    response_model=TemplateBatchResponse,
    summary="Guided Batch: fill locked template from intake CSV",
)
async def guided_batch_csv(
    template_id: str,
    api_key: dict = Depends(require_api_key),
    csv_file: UploadFile = File(..., description="CSV with intake headers from /guided-csv"),
    profile_id: Optional[str] = Form(None, examples=[EX_PROFILE_ID]),
    profile_ids: Optional[str] = Form(
        None,
        description="Comma-separated base profiles (provider/facility) merged into every row",
    ),
    signature_mode: str = Form(
        default="none",
        description="'none' or 'typed' (stamp t:<sig_field> / signer_name overlays)",
    ),
    consent_given: bool = Form(
        default=False,
        description="Required when signature_mode=typed (ESIGN / UETA consent)",
    ),
    signer_name: str = Form(default="", description="Fallback typed signer name"),
    signer_email: str = Form(default="", description="Optional signer email for audit"),
    dpi: int = Form(default=200, ge=150, le=300),
    ai_api_key: Optional[str] = Form(None, examples=[EX_AI_API_KEY]),
    ai_base_url: str = Form(default=EX_AI_BASE_URL, examples=[EX_AI_BASE_URL]),
    ai_model: str = Form(default="gemini-2.5-flash", examples=[EX_AI_MODEL]),
    ai_provider: Optional[str] = Form(None),
):
    """Fill many rows using the locked map (canonical / q: / t: columns).

    Download the header row from ``GET .../guided-csv`` first. Provider
    profiles can be attached once via ``profile_ids``; each CSV row supplies
    the patient + clinical values.
    """
    import csv as _csv
    import io as _io

    # Ensure locked map exists
    _intake_for_template(template_id)

    raw = await csv_file.read()
    try:
        text = raw.decode("utf-8-sig", errors="replace")
        reader = _csv.DictReader(_io.StringIO(text))
        records = []
        for row in reader:
            clean = {
                (k or "").strip(): (v or "").strip()
                for k, v in row.items()
                if k and (v or "").strip()
            }
            if clean:
                records.append(clean)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse CSV: {exc}")

    if not records:
        raise HTTPException(400, "CSV has no data rows")
    if len(records) > 500:
        raise HTTPException(400, "Maximum 500 rows per batch")

    try:
        tpl = _get_service().get(template_id)
    except KeyError:
        raise HTTPException(404, f"Template '{template_id}' not found")

    parsed_ids = (
        [p.strip() for p in profile_ids.split(",") if p.strip()] if profile_ids else None
    )

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
            records=records,
            ai_api_key=resolved_key,
            ai_base_url=resolved_url,
            ai_model=resolved_model,
            dpi=dpi,
            profile_id=profile_id,
            profile_ids=parsed_ids,
            preserve_input=True,
            signature_mode=signature_mode,
            consent_given=consent_given,
            signer_name=signer_name,
            signer_email=signer_email,
            owner_id=api_key.get("id"),
            tier=api_key.get("tier", "free"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Guided batch failed: {exc}")

    return resp


@router.post(
    "/{template_id}/batch",
    response_model=TemplateBatchResponse,
    summary="Batch-fill template with multiple records",
)
async def batch_fill_template(
    template_id: str,
    background_tasks: BackgroundTasks,
    api_key: dict = Depends(require_api_key),
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
            owner_id=api_key.get("id"),
            tier=api_key.get("tier", "free"),
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
