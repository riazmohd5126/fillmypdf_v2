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
  PATCH  /mappings/{fp}            correct field -> canonical entries
  POST   /mappings/{fp}/lock       mark reviewed=true
  POST   /mappings/{fp}/unlock     mark reviewed=false
  POST   /mappings/{fp}/ai-suggest AI-map only this form's unmapped tail
  POST   /mappings/ai-suggest      AI-map the unmapped tail across all drafts
  POST   /mappings/lock-batch      lock many maps at once
  POST   /mappings/build           build a draft from a blank PDF or a shipped form
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from ...config import settings
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

    return {
        "fingerprint": data.get("fingerprint", fp),
        "signature": sig,
        "form_label": data.get("form_label"),
        "reviewed": bool(data.get("reviewed", False)),
        "cached_at": data.get("cached_at"),
        "updated_at": data.get("updated_at"),
        "field_count": len(rows),
        "mapped_count": sum(1 for r in rows if r["canonical"] and r["canonical"] != "other"),
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
    return {"mappings": cache.list_entries()}


@router.get("/catalog", summary="Canonical paths for the review editor dropdown")
async def get_catalog():
    return {"catalog": _catalog()}


# ---------------------------------------------------------------------------
# Read one
# ---------------------------------------------------------------------------

@router.get("/{fp}", summary="Get one canonical mapping (per-field rows)")
async def get_mapping(fp: str):
    return _detail(CanonicalMapCache(), fp)


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
    role: str = ""


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
    summary="Assign a signer role to a signature field",
)
async def patch_signature_role(fp: str, field: str, body: SignatureRoleUpdate):
    sig = _signature_for(fp)
    if not FormSpecCache().set_signature_role(sig, field, body.role):
        raise HTTPException(404, f"Signature field '{field}' not found")
    return FormSpecCache().get(sig).model_dump(mode="json")


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
# Update
# ---------------------------------------------------------------------------

class MappingUpdate(BaseModel):
    # {field: canonical|"other"|"" | {canonical, value?}}  ("" removes mapping)
    updates: Dict[str, object]


@router.patch("/{fp}", summary="Correct field -> canonical entries")
async def patch_mapping(fp: str, body: MappingUpdate):
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


@router.post("/{fp}/lock", summary="Mark a mapping reviewed (locked/authoritative)")
async def lock_mapping(fp: str):
    cache = CanonicalMapCache()
    if not cache.set_reviewed(fp, True):
        raise HTTPException(404, f"Mapping '{fp}' not found")
    _sync_form_spec_reviewed(fp, True)
    return _detail(cache, fp)


@router.post("/{fp}/unlock", summary="Clear the reviewed flag (back to draft)")
async def unlock_mapping(fp: str):
    cache = CanonicalMapCache()
    if not cache.set_reviewed(fp, False):
        raise HTTPException(404, f"Mapping '{fp}' not found")
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
async def lock_batch(body: LockBatch):
    if not body.fingerprints:
        raise HTTPException(400, "No fingerprints provided")
    cache = CanonicalMapCache()
    results = []
    for fp in body.fingerprints:
        ok = bool(cache.set_reviewed(fp, True))
        if ok:
            _sync_form_spec_reviewed(fp, True)
        results.append({"fingerprint": fp, "ok": ok})
    return {"locked": sum(1 for r in results if r["ok"]), "results": results}


# ---------------------------------------------------------------------------
# Build a draft from a blank form (before any patient data)
# ---------------------------------------------------------------------------

@router.post("/build", summary="Build a canonical mapping draft from a blank PDF or shipped form")
async def build_mapping(
    file: Optional[UploadFile] = File(default=None, description="Blank fillable PDF"),
    form_id: Optional[str] = Form(default=None, description="Shipped pa_forms id (filename stem)"),
):
    # Resolve the source PDF.
    tmp_path: Optional[Path] = None
    if file is not None:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "File must be a PDF")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_path = settings.UPLOAD_DIR / f"{ts}_{uuid.uuid4().hex[:8]}_mapbuild.pdf"
        tmp_path.write_bytes(await file.read())
        pdf_path = tmp_path
        form_label = file.filename
    elif form_id:
        pdf_path = _FORMS_DIR / f"{form_id}.pdf"
        if not pdf_path.exists():
            available = [f.stem for f in _FORMS_DIR.glob("*.pdf")] if _FORMS_DIR.exists() else []
            raise HTTPException(404, f"Form '{form_id}' not found. Available: {available}")
        form_label = pdf_path.name
    else:
        raise HTTPException(400, "Provide either a PDF 'file' or a 'form_id'")

    try:
        from ...services.vision_service import VisionService

        resolved_key = (settings.GEMINI_API_KEY or "").strip() or os.getenv("GEMINI_API_KEY", "")
        vs = VisionService(resolved_key, settings.DEFAULT_AI_BASE_URL, settings.DEFAULT_AI_MODEL)

        fields_info = vs._get_fields_with_coords(str(pdf_path))
        if not fields_info:
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
        data = cache.get_full(fp) or data
        data["mappings"] = mappings
        data["form_label"] = form_label
        data.setdefault("signature", sig)
        data = sync_field_kinds(data, fields_info)
        cache.save_full(fp, data)
        FormSpecCache().save(spec)

        return _detail(cache, fp)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
