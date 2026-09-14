"""
Mapping Review + Approve/Lock Routes
====================================
Admin endpoints to review, correct and LOCK the PHI-free canonical field
mapping (``field -> canonical path``) that a form gets on first fill.

A locked (``reviewed=true``) map is honored verbatim by the fill pipeline
(looked up by the form's structure signature), is never overwritten by the AI,
and survives schema/model/label changes.

Endpoints (all admin-only, mounted at /api/v1/mappings):
  GET    /mappings                 list draft + locked maps
  GET    /mappings/catalog         canonical paths for the editor dropdown
  GET    /mappings/{fp}            one map: per-field rows + catalog
  GET    /mappings/{fp}/pdf        blank PDF for side-by-side review
  PATCH  /mappings/{fp}            correct field -> canonical entries
  POST   /mappings/{fp}/lock       mark reviewed=true
  POST   /mappings/{fp}/unlock     mark reviewed=false
  POST   /mappings/{fp}/ai-suggest AI-map only this form's unmapped tail
  POST   /mappings/ai-suggest      AI-map the unmapped tail across all drafts
  POST   /mappings/lock-batch      lock many maps at once
  POST   /mappings/build           build a draft from a blank PDF, library template, or shipped form
  POST   /mappings/{fp}/checklist/ai-suggest   draft a submission checklist from this form's own fields
  PUT    /mappings/{fp}/checklist              replace the checklist (admin edit)

A locked map's checklist (whatever the admin left it as) is synced onto the
linked Template Library manifest at lock time, where it becomes visible to
all users via GET /templates/{id} — but deliberately not surfaced inside the
Guided Fill field-filling flow itself.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ...config import settings
from ...services.template_access import FILLED_PDF_MSG, acroform_has_filled_values
from ...models.pa_canonical import CATALOG, BY_PATH, CATALOG_CHOICES, infer_option_value
from ...services.canonical_field_service import CanonicalFieldService
from ...services.canonical_map_cache import CanonicalMapCache
from ...services.canonical_schema import derive_schema, intake_csv_headers, intake_schema
from ...services.field_classifier import DATA, is_section_title_field
from ...services.form_spec_builder import build_form_spec, promote_unresolved_long_text
from ...services.intake_rules import apply_intake_annotations, sync_field_kinds
from ...services.form_spec_cache import FormSpecCache
from ..dependencies.auth import require_admin

router = APIRouter(
    prefix="/mappings",
    tags=["mappings"],
    dependencies=[Depends(require_admin)],
)

# Blank forms shipped with the app (same dir pa_routes serves from).
_FORMS_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "pa_forms"


def _blank_pdf_dir() -> Path:
    """PHI-free blank PDFs kept for Mapping Review side-by-side preview."""
    path = settings.STORAGE_DIR / "blank_forms"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _blank_pdf_path(fp: str) -> Path:
    return _blank_pdf_dir() / f"{fp}.pdf"


def _persist_blank_pdf(fp: str, pdf_path: Path) -> bool:
    """Copy the blank source PDF next to the map so reopen can preview it."""
    if not pdf_path.is_file():
        return False
    dest = _blank_pdf_path(fp)
    try:
        shutil.copy2(pdf_path, dest)
        return True
    except OSError:
        return False


def _template_id_from_label(label: str) -> str:
    """Same sanitizer the bulk importer uses for template ids."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", (label or "").strip()).strip("_")[:120]


def _template_pdf_path(template_id: str) -> Optional[Path]:
    """Fillable copy if present, otherwise the stored template PDF."""
    if not template_id:
        return None
    from ...repositories.template_repository import TemplateRepository

    repo = TemplateRepository()
    fillable = repo.get_fillable_path(template_id)
    if fillable is not None and fillable.is_file():
        return fillable
    raw = repo.get_pdf_path(template_id)
    if raw is not None and raw.is_file():
        return raw
    return None


def find_template_id(data: Optional[dict]) -> Optional[str]:
    """Resolve a library template for a map (explicit id, then label → id)."""
    data = data or {}
    from ...repositories.template_repository import TemplateRepository

    repo = TemplateRepository()
    tid = (data.get("template_id") or "").strip()
    if tid and repo.exists(tid):
        return tid
    label = (data.get("form_label") or "").strip()
    if not label:
        return None
    guess = _template_id_from_label(label)
    if guess and repo.exists(guess):
        return guess
    stem = Path(label).stem
    if stem != label:
        guess2 = _template_id_from_label(stem)
        if guess2 and repo.exists(guess2):
            return guess2
    label_lc = label.lower().replace("_", " ")
    for manifest in repo.list_all():
        name = (manifest.name or "").lower()
        if name == label.lower() or name == label_lc:
            return manifest.id
    return None


def _resolve_blank_pdf(fp: str, data: Optional[dict] = None) -> Optional[Path]:
    """Return a readable blank PDF for ``fp``, or None.

    Prefer the persisted Mapping Review copy. Then a shipped pa_form. Then
    the Template Library PDF this map was imported from (by ``template_id``
    or ``form_label``).
    """
    stored = _blank_pdf_path(fp)
    if stored.is_file():
        return stored
    if data is None:
        data = CanonicalMapCache().get_full(fp) or {}
    form_id = (data.get("source_form_id") or "").strip()
    if form_id:
        candidate = _FORMS_DIR / f"{form_id}.pdf"
        if candidate.is_file():
            return candidate
    label = (data.get("form_label") or "").strip()
    if label:
        by_name = _FORMS_DIR / label
        if by_name.is_file():
            return by_name
        stem = Path(label).stem
        by_stem = _FORMS_DIR / f"{stem}.pdf"
        if by_stem.is_file():
            return by_stem
    tid = find_template_id(data)
    if tid:
        found = _template_pdf_path(tid)
        if found is not None:
            return found
    return None


def _request_actor(request: Request) -> str:
    user = getattr(request.state, "user", None) or {}
    email = str(user.get("email") or "").strip()
    if email:
        return email
    key = getattr(request.state, "api_key", None) or {}
    return str(key.get("owner") or key.get("name") or "admin").strip() or "admin"


def _stamp_actor(cache: CanonicalMapCache, fp: str, actor: str) -> None:
    data = cache.get_full(fp)
    if not data:
        return
    data["actor"] = actor
    cache.save_full(fp, data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _catalog() -> list[dict]:
    """Canonical paths offered in the review dropdown (plus the 'other' escape)."""
    rows = []
    for f in CATALOG:
        row = {
            "path": f.path,
            "type": f.type,
            "required": bool(getattr(f, "required", False)),
        }
        if getattr(f, "choices", ()):
            row["choices"] = [{"value": v, "label": l} for v, l in f.choices]
        rows.append(row)
    rows.append({"path": "other", "type": "text", "required": False})
    return rows


def _ordered_field_names(labels: Dict[str, str], mappings: Dict[str, dict]) -> list[str]:
    """Preserve PDF reading order already stored in the cache dicts.

    At build time, ``_get_fields_with_coords`` sorts widgets page → y → x and
    that order is written into ``field_labels`` / ``mappings``.  Re-sorting by
    field name (numeric then lexical) scrambled that layout and made manual
    review hard.  Walk labels first (primary source of reading order), then
    append any mapping-only keys that somehow lack a label.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for field in labels.keys():
        if field not in seen:
            ordered.append(field)
            seen.add(field)
    for field in mappings.keys():
        if field not in seen:
            ordered.append(field)
            seen.add(field)
    return ordered


def _detail(cache: CanonicalMapCache, fp: str) -> dict:
    data = cache.get_full(fp)
    if data is None:
        raise HTTPException(404, f"Mapping '{fp}' not found")

    # Only data fields belong in the canonical table. Checkboxes, narratives,
    # signatures and form-specific tables are reviewed in their own sections.
    # Gated dependents (How Long) keep rule metadata for the Questions tab —
    # Canonical tab shows plain paths only (no branch badges).
    sig = data.get("signature")
    spec = FormSpecCache().get(sig) if sig else None
    # Upgrade stale FormSpecs (typed /Tx signatures) when Mapping Review opens.
    if spec is not None:
        try:
            from ...services.form_spec_refresh import (
                needs_signatures_rebuild,
                rebuild_form_spec_for_signatures,
            )
            if needs_signatures_rebuild(spec):
                rebuilt = rebuild_form_spec_for_signatures(
                    sig,
                    form_label=data.get("form_label") or spec.form_label,
                    entry=data,
                )
                if rebuilt is not None:
                    spec = rebuilt
                    data = cache.get_full(fp) or data
        except Exception as exc:
            print(f"  ⚠️  mapping detail FormSpec refresh skipped: {exc}")
        # Backfill missing stamp boxes from blank PDF AcroForm (non-destructive).
        try:
            _ensure_signature_placements(fp, overwrite=False)
            spec = FormSpecCache().get(sig) or spec
        except Exception:
            pass

    labels: Dict[str, str] = data.get("field_labels", {}) or {}
    mappings: Dict[str, dict] = data.get("mappings", {}) or {}
    field_names = _ordered_field_names(labels, mappings)
    field_types: Dict[str, str] = data.get("field_types", {}) or {}
    kinds: Dict[str, str] = data.get("field_kinds", {}) or {}
    table_keys = set(spec.table_field_keys) if spec is not None else set()
    from ...services.form_spec_builder import signature_field_keys
    sig_keys = signature_field_keys(spec) if spec is not None else set()

    rows = []
    gated_rows = []
    for field in field_names:
        if kinds and kinds.get(field, DATA) != DATA:
            continue
        if field in table_keys or field in sig_keys:
            continue
        label = labels.get(field, "")
        if is_section_title_field(field, label):
            continue
        m = mappings.get(field) if isinstance(mappings.get(field), dict) else {}
        row = {
            "field": field,
            "label": label,
            "canonical": (m or {}).get("canonical"),
            "value": (m or {}).get("value"),
            "confidence": (m or {}).get("confidence"),
            "source": (m or {}).get("source"),
            "field_type": field_types.get(field, ""),
            "linked_field": (m or {}).get("linked_field"),
            "conditional": (m or {}).get("conditional"),
            "rule": (m or {}).get("rule"),
        }
        if row.get("rule") or row.get("linked_field") or row.get("conditional"):
            gated_rows.append(row)
        rows.append(row)

    tid = find_template_id(data)
    return {
        "fingerprint": data.get("fingerprint", fp),
        "signature": sig,
        "form_label": data.get("form_label"),
        "source_form_id": data.get("source_form_id"),
        "template_id": tid or data.get("template_id"),
        "source_path": data.get("source_path"),
        "actor": data.get("actor"),
        "reviewed": bool(data.get("reviewed", False)),
        "cached_at": data.get("cached_at"),
        "updated_at": data.get("updated_at"),
        "field_count": len(rows),
        "mapped_count": sum(1 for r in rows if r["canonical"] and r["canonical"] != "other"),
        "has_blank_pdf": _resolve_blank_pdf(fp, data) is not None,
        "template_pdf_url": f"/api/v1/templates/{tid}/pdf" if tid else None,
        "rows": rows,
        "gated_rows": gated_rows,
        "catalog": _catalog(),
        "form_spec": spec.model_dump(mode="json") if spec else None,
    }


# ---------------------------------------------------------------------------
# AI enrichment (fill the unmapped tail; PHI-free labels only; never auto-locks)
# ---------------------------------------------------------------------------

def _ai_service() -> CanonicalFieldService:
    """Build the canonical service with the server AI key, or raise a clear error.

    The AI step only ever sees blank-form labels (schema, not patient data), so
    it stays PHI-free even on a cloud model. It is gated by the same switch the
    fill path uses (``CANONICAL_AI_FALLBACK``) and requires a configured key.
    """
    if not settings.CANONICAL_AI_FALLBACK:
        raise HTTPException(412, "AI fallback is disabled (CANONICAL_AI_FALLBACK=false).")
    resolved_key = (settings.GEMINI_API_KEY or "").strip() or os.getenv("GEMINI_API_KEY", "")
    svc = CanonicalFieldService(
        resolved_key, settings.DEFAULT_AI_BASE_URL, settings.DEFAULT_AI_MODEL
    )
    if not svc._ai_ready():
        raise HTTPException(409, "No AI key configured. Set GEMINI_API_KEY to use AI suggest.")
    return svc


def _ai_enrich_one(cache: CanonicalMapCache, svc: CanonicalFieldService, fp: str) -> dict:
    """Run the AI mapper over ONLY the unmapped tail of one draft map.

    A field is a candidate when it is not already resolved to a real catalog
    path AND was not set by a human (``source='manual'``). Manual and
    deterministic (``catalog``) decisions are never overwritten, and the
    ``reviewed`` flag is left untouched — a human still confirms before locking.
    """
    data = cache.get_full(fp)
    if data is None:
        raise HTTPException(404, f"Mapping '{fp}' not found")

    labels: Dict[str, str] = data.get("field_labels", {}) or {}
    mappings: Dict[str, dict] = data.get("mappings", {}) or {}

    field_names = list(labels.keys()) + [k for k in mappings if k not in labels]
    # AcroForm types captured at build time (e.g. "/Btn") so Gemini sees checkbox
    # vs text. Older cache entries may lack this — fall back to "".
    field_types: Dict[str, str] = data.get("field_types", {}) or {}
    kinds: Dict[str, str] = data.get("field_kinds", {}) or {}
    unresolved = []
    for name in field_names:
        # Checkboxes, narratives and signatures aren't catalog-mappable, so
        # sending them to Gemini only burns tokens to be told "other".
        if kinds and kinds.get(name, DATA) != DATA:
            continue
        m = mappings.get(name) if isinstance(mappings.get(name), dict) else {}
        if (m or {}).get("source") == "manual":
            continue
        canon = (m or {}).get("canonical")
        # Already decided — real path OR Gemini/manual "other". Don't re-send.
        # Exception: catalog path without value on a checkbox/enum → ask AI to
        # fill in the option value (field → (path, value)).
        needs_value = (
            canon in BY_PATH
            and not (m or {}).get("value")
            and (
                "/Btn" in field_types.get(name, "")
                or "::" in name  # radio option key without a choice value yet
            )
            and bool(CATALOG_CHOICES.get(canon))
        )
        if (canon in BY_PATH or canon == "other") and not needs_value:
            continue
        # Synthetic field dict: name may be "undefined_3::Male" for radios.
        export = name.split("::", 1)[1] if "::" in name else None
        acro = name.split("::", 1)[0]
        unresolved.append({
            "name": acro,
            "type": field_types.get(name, ""),
            "export_value": export,
            "_radio_group": bool(export),
            "_map_key": name,
        })

    if not unresolved:
        return {"fingerprint": fp, "candidates": 0, "added": 0}

    try:
        ai_map = svc._ai_map(unresolved, labels)
    except Exception as exc:  # network / model errors shouldn't 500 the batch
        return {"fingerprint": fp, "candidates": len(unresolved), "added": 0,
                "error": str(exc)}

    # Remap AI keys: model may return acro name or name::export.
    key_alias = {}
    for u in unresolved:
        mk = u.get("_map_key") or u.get("name")
        key_alias[mk] = mk
        key_alias[u.get("name")] = mk

    added = 0
    for name, entry in (ai_map or {}).items():
        if not isinstance(entry, dict):
            continue
        canon = entry.get("canonical")
        target = key_alias.get(name, name)
        # Persist real catalog hits AND "other" (nothing-fits) so the UI stops
        # showing them as unreviewed and we don't re-query Gemini for them.
        if canon in BY_PATH or canon == "other":
            if canon in BY_PATH and not entry.get("value"):
                opt = infer_option_value(
                    canon, labels.get(target), labels.get(name),
                    target.split("::", 1)[1] if "::" in target else None,
                )
                if opt:
                    entry = dict(entry)
                    entry["value"] = opt
            mappings[target] = entry  # entry already carries source='ai'
            added += 1
    if added:
        data["mappings"] = mappings
        cache.save_full(fp, data)
    return {"fingerprint": fp, "candidates": len(unresolved), "added": added}


# ---------------------------------------------------------------------------
# List / catalog
# ---------------------------------------------------------------------------

@router.get("", summary="List canonical mapping drafts and locked maps")
async def list_mappings():
    cache = CanonicalMapCache()
    entries = cache.list_entries()
    from ...repositories.template_repository import TemplateRepository

    known = {m.id for m in TemplateRepository().list_all()}
    for e in entries:
        tid = (e.get("template_id") or "").strip()
        if not tid and e.get("form_label"):
            tid = _template_id_from_label(e["form_label"])
        if tid:
            e["template_id"] = tid
            if not e.get("actor") and tid in known:
                e["actor"] = "import"
    return {"mappings": entries}


@router.get("/catalog", summary="Canonical paths for the review editor dropdown")
async def get_catalog():
    return {"catalog": _catalog()}


# ---------------------------------------------------------------------------
# Read one
# ---------------------------------------------------------------------------

@router.get("/{fp}", summary="Get one canonical mapping (per-field rows)")
async def get_mapping(fp: str):
    return _detail(CanonicalMapCache(), fp)


@router.get("/{fp}/pdf", summary="Blank PDF for side-by-side Mapping Review")
async def get_mapping_pdf(fp: str):
    """Serve the blank (PHI-free) form PDF used when this map was built.

    Used by the review UI as a left-pane preview. Auth is the same admin
    API key as other /mappings routes — the UI fetches as a blob (iframes
    cannot send X-API-Key).
    """
    cache = CanonicalMapCache()
    data = cache.get_full(fp)
    if data is None:
        raise HTTPException(404, f"Mapping '{fp}' not found")
    path = _resolve_blank_pdf(fp, data)
    if path is None:
        raise HTTPException(
            404,
            "No blank PDF stored for this map. Rebuild the draft from the "
            "PDF or a shipped form to enable side-by-side preview.",
        )
    # Backfill a persisted copy when we resolved a shipped form or template.
    if not _blank_pdf_path(fp).is_file():
        _persist_blank_pdf(fp, path)
        path = _blank_pdf_path(fp) if _blank_pdf_path(fp).is_file() else path
    filename = data.get("form_label") or f"{fp}.pdf"
    if not str(filename).lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=str(filename),
    )


# ---------------------------------------------------------------------------
# Intake schema (guided web form / CSV) derived from a map
# ---------------------------------------------------------------------------


@router.get("/{fp}/schema", summary="Canonical intake schema derived from this map")
async def get_mapping_schema(fp: str):
    data = CanonicalMapCache().get_full(fp)
    if data is None:
        raise HTTPException(404, f"Mapping '{fp}' not found")
    spec = FormSpecCache().get(data.get("signature") or "")
    return {
        "fingerprint": data.get("fingerprint", fp),
        "form_label": data.get("form_label"),
        "reviewed": bool(data.get("reviewed", False)),
        "schema": derive_schema(data.get("mappings", {})),
        "intake": intake_schema(data.get("mappings", {}), spec),
    }


@router.get("/{fp}/schema.csv", summary="Batch CSV template (canonical + question columns)")
async def get_mapping_schema_csv(fp: str):
    data = CanonicalMapCache().get_full(fp)
    if data is None:
        raise HTTPException(404, f"Mapping '{fp}' not found")
    spec = FormSpecCache().get(data.get("signature") or "")
    headers = intake_csv_headers(intake_schema(data.get("mappings", {}), spec))
    body = ("\ufeff" + ",".join(headers) + "\n").encode("utf-8")
    fn = (data.get("form_label") or fp).rsplit(".", 1)[0] + "_template.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


# ---------------------------------------------------------------------------
# Form spec (per-form questions, narratives, signatures — not canonicalized)
# ---------------------------------------------------------------------------

def _signature_for(fp: str) -> str:
    data = CanonicalMapCache().get_full(fp)
    if data is None:
        raise HTTPException(404, f"Mapping '{fp}' not found")
    sig = data.get("signature")
    if not sig:
        raise HTTPException(409, f"Mapping '{fp}' has no structure signature; rebuild it")
    return str(sig)


class QuestionUpdate(BaseModel):
    question: Optional[str] = None
    input: Optional[str] = None
    canonical_hint: Optional[str] = None


class SignatureRoleUpdate(BaseModel):
    """Patch a signature field's role and/or locked stamp placement."""

    role: Optional[str] = None
    placement: Optional[dict] = None  # {page_index,x_pct,y_pct,width_pct,height_pct}
    clear_placement: bool = False


def _ensure_signature_placements(fp: str, *, overwrite: bool = False) -> list[str]:
    """Fill missing SignatureField.placement from the blank PDF AcroForm.

    Returns labels still missing a stamp box after the attempt.
    """
    from ...services.esign_service import (
        attach_placements_to_form_spec,
        missing_signature_placements,
    )
    from ...services.vision_service import VisionService

    cache = CanonicalMapCache()
    data = cache.get_full(fp) or {}
    try:
        sig = _signature_for(fp)
    except HTTPException:
        return []
    spec = FormSpecCache().get(sig)
    if spec is None:
        return []
    pdf = _resolve_blank_pdf(fp, data)
    if pdf is None or not pdf.is_file():
        return missing_signature_placements(spec)

    vs = VisionService(
        (settings.GEMINI_API_KEY or "").strip() or "-",
        settings.DEFAULT_AI_BASE_URL,
        settings.DEFAULT_AI_MODEL,
    )
    try:
        fields_info = vs._get_fields_with_coords(str(pdf))
    except Exception:
        return missing_signature_placements(spec)
    attach_placements_to_form_spec(spec, fields_info, pdf, overwrite=overwrite)
    FormSpecCache()._force_save(spec)
    return missing_signature_placements(spec)


@router.get("/{fp}/form-spec", summary="This form's questions, narratives and signatures")
async def get_form_spec(fp: str):
    """Return FormSpec, upgrading stale signature caches when unlocked."""
    from ...services.form_spec_refresh import (
        needs_signatures_rebuild,
        rebuild_form_spec_for_signatures,
    )

    sig = _signature_for(fp)
    spec = FormSpecCache().get(sig)
    if spec is None:
        raise HTTPException(404, "No form spec built for this form yet — rebuild it")

    if needs_signatures_rebuild(spec):
        try:
            entry = CanonicalMapCache().get_full(fp)
            rebuilt = rebuild_form_spec_for_signatures(
                sig,
                form_label=(entry or {}).get("form_label") or spec.form_label,
                entry=entry,
            )
            if rebuilt is not None:
                spec = rebuilt
                print(f"  🔄  Mapping Review rebuilt FormSpec {sig} "
                      f"(signatures_version bump)")
        except Exception as exc:
            print(f"  ⚠️  form-spec signature refresh skipped: {exc}")

    # Fill any missing stamp boxes from AcroForm (does not overwrite locked ones).
    try:
        _ensure_signature_placements(fp, overwrite=False)
        spec = FormSpecCache().get(sig) or spec
    except Exception:
        pass

    return spec.model_dump(mode="json")


@router.patch(
    "/{fp}/form-spec/questions/{question_id}",
    summary="Correct a question's wording, input type or optional canonical hint",
)
async def patch_question(fp: str, question_id: str, body: QuestionUpdate):
    if body.canonical_hint not in (None, "") and body.canonical_hint not in BY_PATH:
        raise HTTPException(400, f"Unknown canonical path: {body.canonical_hint}")
    if body.input not in (None, "radio", "checkbox"):
        raise HTTPException(400, "input must be 'radio' or 'checkbox'")
    sig = _signature_for(fp)
    ok = FormSpecCache().update_question(
        sig,
        question_id,
        question=body.question,
        input_type=body.input,
        canonical_hint=body.canonical_hint,
    )
    if not ok:
        raise HTTPException(404, f"Question '{question_id}' not found")
    return FormSpecCache().get(sig).model_dump(mode="json")


@router.patch(
    "/{fp}/form-spec/signatures/{field}",
    summary="Assign signer role and/or locked e-sign stamp placement",
)
async def patch_signature_role(fp: str, field: str, body: SignatureRoleUpdate):
    sig = _signature_for(fp)
    # Role-only clients always send ``role``; placement-only omit it.
    has_role = "role" in body.model_fields_set if hasattr(body, "model_fields_set") else body.role is not None
    ok = FormSpecCache().update_signature(
        sig,
        field,
        role=body.role if has_role else None,
        placement=body.placement,
        clear_placement=body.clear_placement,
    )
    if not ok:
        raise HTTPException(404, f"Signature field '{field}' not found")
    return FormSpecCache().get(sig).model_dump(mode="json")


@router.post(
    "/{fp}/form-spec/signatures/placements/from-acro",
    summary="Fill signature stamp boxes from AcroForm field geometry",
)
async def apply_signature_placements_from_acro(
    fp: str,
    overwrite: bool = False,
):
    """Derive locked placements from the blank PDF's AcroForm widgets."""
    missing = _ensure_signature_placements(fp, overwrite=overwrite)
    sig = _signature_for(fp)
    spec = FormSpecCache().get(sig)
    if spec is None:
        raise HTTPException(404, "No form spec built for this form yet")
    return {
        "missing": missing,
        "form_spec": spec.model_dump(mode="json"),
    }


class QuestionMerge(BaseModel):
    question_ids: list[str]
    question: Optional[str] = None  # new shared header; inferred if omitted
    input: Optional[str] = None     # 'radio' (dropdown) or 'checkbox'


@router.post(
    "/{fp}/form-spec/questions/merge",
    summary="Merge several question cards into one (e.g. New + Continuation → Type of therapy)",
)
async def merge_questions(fp: str, body: QuestionMerge):
    if len(body.question_ids or []) < 2:
        raise HTTPException(400, "Select at least two questions to merge")
    if body.input not in (None, "radio", "checkbox"):
        raise HTTPException(400, "input must be 'radio' or 'checkbox'")
    sig = _signature_for(fp)
    spec = FormSpecCache().merge_questions(
        sig,
        body.question_ids,
        question=body.question,
        input_type=body.input,
    )
    if spec is None:
        raise HTTPException(404, "One or more question ids not found")
    return spec.model_dump(mode="json")


@router.post(
    "/{fp}/form-spec/recluster",
    summary="Auto-merge adjacent header-less checkbox cards (Question === Option)",
)
async def recluster_form_spec(fp: str):
    """Fixes duplicate solo cards like Male/Female or New/Continuation therapy."""
    sig = _signature_for(fp)
    spec = FormSpecCache().recluster_solos(sig)
    if spec is None:
        raise HTTPException(404, "No form spec built for this form yet")
    return spec.model_dump(mode="json")


@router.post("/{fp}/form-spec/lock", summary="Mark this form's spec reviewed")
async def lock_form_spec(fp: str, reviewed: bool = True):
    sig = _signature_for(fp)
    if not FormSpecCache().set_reviewed(sig, reviewed):
        raise HTTPException(404, "No form spec built for this form yet")
    return FormSpecCache().get(sig).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Submission checklist — drafted from this form's own checkbox/question
# fields, admin-edited, synced onto the Template Library manifest at lock.
# ---------------------------------------------------------------------------

class ChecklistUpdate(BaseModel):
    checklist: list[str]


def _checklist_service() -> "ChecklistService":
    from ...services.checklist_service import ChecklistService

    if not settings.CANONICAL_AI_FALLBACK:
        raise HTTPException(412, "AI fallback is disabled (CANONICAL_AI_FALLBACK=false).")
    resolved_key = (settings.GEMINI_API_KEY or "").strip() or os.getenv("GEMINI_API_KEY", "")
    if not resolved_key:
        raise HTTPException(409, "No AI key configured. Set GEMINI_API_KEY to use AI suggest.")
    return ChecklistService(
        api_key=resolved_key, base_url=settings.DEFAULT_AI_BASE_URL, model=settings.DEFAULT_AI_MODEL
    )


@router.post(
    "/{fp}/checklist/ai-suggest",
    summary="Draft a submission checklist from this form's own checkbox/question fields",
)
async def ai_suggest_checklist(fp: str):
    from ...services.checklist_service import ChecklistError

    sig = _signature_for(fp)
    spec = FormSpecCache().get(sig)
    if spec is None:
        raise HTTPException(404, "No form spec built for this form yet — rebuild it")
    svc = _checklist_service()
    try:
        items = svc.draft(spec)
    except ChecklistError as exc:
        raise HTTPException(502, str(exc))
    if not FormSpecCache().set_checklist(sig, items):
        raise HTTPException(404, "No form spec built for this form yet")
    return FormSpecCache().get(sig).model_dump(mode="json")


@router.put("/{fp}/checklist", summary="Replace the submission checklist (admin edit)")
async def update_checklist(fp: str, body: ChecklistUpdate):
    sig = _signature_for(fp)
    if not FormSpecCache().set_checklist(sig, body.checklist):
        raise HTTPException(404, "No form spec built for this form yet")
    return FormSpecCache().get(sig).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

class MappingUpdate(BaseModel):
    # {field: canonical|"other"|"" | {canonical, value?}}  ("" removes mapping)
    updates: Dict[str, object]


@router.patch("/{fp}", summary="Correct field -> canonical entries")
async def patch_mapping(fp: str, body: MappingUpdate, request: Request):
    if not body.updates:
        raise HTTPException(400, "No updates provided")
    valid = set(BY_PATH.keys()) | {"other", "", None}
    bad = []
    for k, v in body.updates.items():
        if isinstance(v, dict):
            canon = v.get("canonical")
            if canon not in valid:
                bad.append(f"{k}={canon}")
        elif v not in valid:
            bad.append(f"{k}={v}")
    if bad:
        raise HTTPException(400, f"Unknown canonical path(s): {bad}")

    cache = CanonicalMapCache()
    if not cache.update_fields(fp, body.updates):
        raise HTTPException(404, f"Mapping '{fp}' not found")
    _stamp_actor(cache, fp, _request_actor(request))
    return _detail(cache, fp)


# ---------------------------------------------------------------------------
# Lock / unlock
# ---------------------------------------------------------------------------

def _sync_form_spec_reviewed(fp: str, reviewed: bool) -> None:
    """Keep the per-form question spec in lockstep with the canonical map."""
    try:
        FormSpecCache().set_reviewed(_signature_for(fp), reviewed)
    except HTTPException:
        pass  # no structure signature / no spec yet — canonical lock still wins


def _sync_checklist_to_manifest(fp: str, data: dict) -> None:
    """Publish the admin's finalized checklist onto the linked Template
    Library manifest so it becomes visible to all users (GET /templates/{id})
    the moment the map is locked. Best-effort: a template that was never
    imported into the library (no template_id resolvable) simply has no
    manifest to sync onto, and that's fine — nothing to fix there."""
    try:
        sig = _signature_for(fp)
    except HTTPException:
        return
    spec = FormSpecCache().get(sig)
    if spec is None:
        return
    tid = find_template_id(data)
    if not tid:
        return
    from ...repositories.template_repository import TemplateRepository

    repo = TemplateRepository()
    manifest = repo.get(tid)
    if manifest is None:
        return
    manifest.checklist = list(spec.checklist)
    repo.save_manifest_only(manifest)


@router.post("/{fp}/lock", summary="Mark a mapping reviewed (locked/authoritative)")
async def lock_mapping(fp: str, request: Request):
    cache = CanonicalMapCache()
    if not cache.set_reviewed(fp, True):
        raise HTTPException(404, f"Mapping '{fp}' not found")
    _stamp_actor(cache, fp, _request_actor(request))
    # Lock stamp boxes with the map: derive any missing placements from AcroForm.
    missing = []
    try:
        missing = _ensure_signature_placements(fp, overwrite=False)
    except Exception as exc:
        print(f"  ⚠️  signature placement lock enrich skipped: {exc}")
    _sync_form_spec_reviewed(fp, True)
    _sync_checklist_to_manifest(fp, cache.get_full(fp) or {})
    detail = _detail(cache, fp)
    if missing:
        detail["signature_placement_warnings"] = missing
    return detail


@router.post("/{fp}/unlock", summary="Clear the reviewed flag (back to draft)")
async def unlock_mapping(fp: str, request: Request):
    cache = CanonicalMapCache()
    if not cache.set_reviewed(fp, False):
        raise HTTPException(404, f"Mapping '{fp}' not found")
    _stamp_actor(cache, fp, _request_actor(request))
    _sync_form_spec_reviewed(fp, False)
    return _detail(cache, fp)


# ---------------------------------------------------------------------------
# AI suggest (enrich the unmapped tail; still requires human review to lock)
# ---------------------------------------------------------------------------

class LockBatch(BaseModel):
    fingerprints: list[str]


@router.post("/ai-suggest", summary="AI-map the unmapped tail across draft maps")
async def ai_suggest_all(scope: str = "drafts"):
    """Run the AI mapper over every eligible map's unmapped tail.

    ``scope=drafts`` (default) only touches unreviewed maps; ``scope=all`` also
    re-checks locked maps' tails (still never flips ``reviewed``). Deterministic
    and manual mappings are always preserved.
    """
    svc = _ai_service()
    cache = CanonicalMapCache()
    results = []
    total_added = 0
    for entry in cache.list_entries():
        if scope != "all" and entry.get("reviewed"):
            continue
        r = _ai_enrich_one(cache, svc, entry["fingerprint"])
        total_added += r.get("added", 0)
        results.append(r)
    return {"scope": scope, "forms": len(results), "total_added": total_added,
            "results": results}


@router.post("/{fp}/ai-suggest", summary="AI-map only this form's unmapped tail")
async def ai_suggest_one(fp: str):
    svc = _ai_service()
    cache = CanonicalMapCache()
    result = _ai_enrich_one(cache, svc, fp)
    detail = _detail(cache, fp)
    detail["ai_suggest"] = result
    return detail


@router.post("/lock-batch", summary="Lock (approve) many mappings at once")
async def lock_batch(body: LockBatch, request: Request):
    if not body.fingerprints:
        raise HTTPException(400, "No fingerprints provided")
    cache = CanonicalMapCache()
    actor = _request_actor(request)
    results = []
    for fp in body.fingerprints:
        ok = bool(cache.set_reviewed(fp, True))
        if ok:
            _stamp_actor(cache, fp, actor)
            try:
                _ensure_signature_placements(fp, overwrite=False)
            except Exception:
                pass
            _sync_form_spec_reviewed(fp, True)
            _sync_checklist_to_manifest(fp, cache.get_full(fp) or {})
        results.append({"fingerprint": fp, "ok": ok})
    return {"locked": sum(1 for r in results if r["ok"]), "results": results}


# ---------------------------------------------------------------------------
# Build a draft from a blank form (before any patient data)
# ---------------------------------------------------------------------------

@router.post("/build", summary="Build a canonical mapping draft from a blank PDF, library template, or shipped form")
async def build_mapping(
    request: Request,
    file: Optional[UploadFile] = File(default=None, description="Blank fillable PDF"),
    form_id: Optional[str] = Form(default=None, description="Shipped pa_forms id (filename stem)"),
    template_id: Optional[str] = Form(
        default=None,
        description="Template Library id (clinic upload or catalog entry)",
    ),
):
    # Resolve the source PDF.
    tmp_path: Optional[Path] = None
    source_form_id: Optional[str] = None
    source_template_id: Optional[str] = (template_id or "").strip() or None
    if file is not None and file.filename:
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "File must be a PDF")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_path = settings.UPLOAD_DIR / f"{ts}_{uuid.uuid4().hex[:8]}_mapbuild.pdf"
        tmp_path.write_bytes(await file.read())
        pdf_path = tmp_path
        form_label = file.filename
        if acroform_has_filled_values(pdf_path):
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(400, FILLED_PDF_MSG)
    elif source_template_id:
        from ...repositories.template_repository import TemplateRepository

        repo = TemplateRepository()
        if not repo.exists(source_template_id):
            raise HTTPException(404, f"Template '{source_template_id}' not found")
        pdf_path = _template_pdf_path(source_template_id)
        if pdf_path is None:
            raise HTTPException(404, f"Template '{source_template_id}' has no PDF on disk")
        if acroform_has_filled_values(pdf_path):
            raise HTTPException(400, FILLED_PDF_MSG)
        manifest = repo.get(source_template_id)
        form_label = (getattr(manifest, "name", None) or source_template_id)
    elif form_id:
        pdf_path = _FORMS_DIR / f"{form_id}.pdf"
        if not pdf_path.exists():
            available = [f.stem for f in _FORMS_DIR.glob("*.pdf")] if _FORMS_DIR.exists() else []
            raise HTTPException(404, f"Form '{form_id}' not found. Available: {available}")
        form_label = pdf_path.name
        source_form_id = form_id
    else:
        raise HTTPException(
            400,
            "Provide a PDF 'file', a library 'template_id', or a shipped 'form_id'",
        )

    try:
        from ...services.vision_service import VisionService

        resolved_key = (settings.GEMINI_API_KEY or "").strip() or os.getenv("GEMINI_API_KEY", "")
        vs = VisionService(resolved_key, settings.DEFAULT_AI_BASE_URL, settings.DEFAULT_AI_MODEL)

        fields_info = vs._get_fields_with_coords(str(pdf_path))
        convert_error = ""
        if not fields_info and source_template_id:
            try:
                from ...services.template_service import TemplateService

                converted = TemplateService()._ensure_fillable(source_template_id)
                if converted is not None and converted.is_file():
                    pdf_path = converted
                    fields_info = vs._get_fields_with_coords(str(pdf_path))
                if not fields_info:
                    convert_error = "the converter found no form fields on its pages"
            except Exception as exc:
                print(f"  ⚠️  fillable convert before map-build skipped: {exc}")
                convert_error = f"the flat-to-fillable converter failed ({exc})"
        if not fields_info:
            if source_template_id:
                reason = convert_error or "it has no AcroForm fields"
                raise HTTPException(
                    400,
                    f"'{form_label}' cannot be mapped yet because {reason}. "
                    "This form is a flat PDF, so it must be made fillable first: "
                    "run it through Make Fillable, then upload the result as the "
                    "template. Scanned or image-only pages cannot be mapped.",
                )
            raise HTTPException(400, "No fillable AcroForm fields found in this PDF")

        # Richest labels available — reuses the cached Gemini pass when the form
        # has been through extract, so the question/section structure survives.
        label_data = vs.rich_label_data(str(pdf_path), fields_info)
        field_labels = vs._flatten_field_labels(fields_info, label_data)

        svc = vs._canonical_service
        cache = svc._cache

        # Build (or reuse a locked) canonical map — data fields only.
        mappings = svc.map_fields(fields_info, field_labels, label_data=label_data)

        # Locate the entry we just built / the locked one for this structure.
        sig = cache.signature(fields_info)
        locked = cache.get_by_signature(sig)
        if locked is not None:
            fp = locked.get("fingerprint")
        else:
            fp = cache.fingerprint(fields_info, field_labels, model=vs.model or "")

        # Stamp a human-friendly form label for the listing.
        data = cache.get_full(fp) or {}
        data["form_label"] = form_label
        if source_form_id:
            data["source_form_id"] = source_form_id
        if source_template_id:
            data["template_id"] = source_template_id
        data.setdefault("signature", sig)
        cache.save_full(fp, data)

        # The other half: this form's own questions, narratives and signatures.
        spec = build_form_spec(
            fields_info,
            label_data,
            signature=sig,
            form_label=form_label,
            widget_key=vs._widget_key,
        )
        promote_unresolved_long_text(
            spec,
            fields_info,
            label_data,
            svc.unresolved_keys(fields_info, mappings),
            widget_key=vs._widget_key,
        )
        # linked_field / conditional → unlock rules for Guided Fill (How Long
        # stays visible but locked until Yes is selected on the parent question).
        mappings, spec = apply_intake_annotations(
            mappings, fields_info, label_data, spec, widget_key=vs._widget_key,
        )
        # Lock e-sign stamp boxes from AcroForm geometry at build time.
        try:
            from ...services.esign_service import attach_placements_to_form_spec

            attach_placements_to_form_spec(spec, fields_info, pdf_path)
        except Exception as exc:
            print(f"  ⚠️  signature placement attach skipped: {exc}")
        data = cache.get_full(fp) or data
        data["mappings"] = mappings
        data["form_label"] = form_label
        if source_form_id:
            data["source_form_id"] = source_form_id
        if source_template_id:
            data["template_id"] = source_template_id
        data.setdefault("signature", sig)
        data = sync_field_kinds(data, fields_info)
        # Keep a blank copy for side-by-side review (before tmp upload is deleted).
        data["has_blank_pdf"] = _persist_blank_pdf(fp, pdf_path)
        data["actor"] = _request_actor(request)
        cache.save_full(fp, data)
        FormSpecCache().save(spec)

        return _detail(cache, fp)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
