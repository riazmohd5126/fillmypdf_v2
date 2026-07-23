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
from ...models.pa_canonical import CATALOG, BY_PATH
from ...services.canonical_map_cache import CanonicalMapCache
from ...services.canonical_schema import derive_schema, schema_csv_headers
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
    rows = [
        {"path": f.path, "type": f.type, "required": bool(getattr(f, "required", False))}
        for f in CATALOG
    ]
    rows.append({"path": "other", "type": "text", "required": False})
    return rows


def _field_sort_key(field: str):
    """Sort numeric field names numerically, everything else lexically after."""
    return (0, int(field)) if str(field).isdigit() else (1, str(field))


def _detail(cache: CanonicalMapCache, fp: str) -> dict:
    data = cache.get_full(fp)
    if data is None:
        raise HTTPException(404, f"Mapping '{fp}' not found")

    labels: Dict[str, str] = data.get("field_labels", {}) or {}
    mappings: Dict[str, dict] = data.get("mappings", {}) or {}

    # Universe of fields = every labeled field plus any mapping-only field.
    field_names = set(labels.keys()) | set(mappings.keys())
    rows = []
    for field in sorted(field_names, key=_field_sort_key):
        m = mappings.get(field) if isinstance(mappings.get(field), dict) else {}
        rows.append({
            "field": field,
            "label": labels.get(field, ""),
            "canonical": (m or {}).get("canonical"),
            "confidence": (m or {}).get("confidence"),
            "source": (m or {}).get("source"),
        })

    return {
        "fingerprint": data.get("fingerprint", fp),
        "signature": data.get("signature"),
        "form_label": data.get("form_label"),
        "reviewed": bool(data.get("reviewed", False)),
        "cached_at": data.get("cached_at"),
        "updated_at": data.get("updated_at"),
        "field_count": len(field_names),
        "mapped_count": sum(1 for r in rows if r["canonical"] and r["canonical"] != "other"),
        "rows": rows,
        "catalog": _catalog(),
    }


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
    return {
        "fingerprint": data.get("fingerprint", fp),
        "form_label": data.get("form_label"),
        "reviewed": bool(data.get("reviewed", False)),
        "schema": derive_schema(data.get("mappings", {})),
    }


@router.get("/{fp}/schema.csv", summary="Batch CSV template (canonical-path headers)")
async def get_mapping_schema_csv(fp: str):
    data = CanonicalMapCache().get_full(fp)
    if data is None:
        raise HTTPException(404, f"Mapping '{fp}' not found")
    headers = schema_csv_headers(derive_schema(data.get("mappings", {})))
    body = ("\ufeff" + ",".join(headers) + "\n").encode("utf-8")
    fn = (data.get("form_label") or fp).rsplit(".", 1)[0] + "_template.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

class MappingUpdate(BaseModel):
    # {field_name: canonical_path | "other" | ""}  ("" removes the mapping)
    updates: Dict[str, Optional[str]]


@router.patch("/{fp}", summary="Correct field -> canonical entries")
async def patch_mapping(fp: str, body: MappingUpdate):
    if not body.updates:
        raise HTTPException(400, "No updates provided")
    valid = set(BY_PATH.keys()) | {"other", "", None}
    bad = [
        f"{k}={v}" for k, v in body.updates.items()
        if v not in valid
    ]
    if bad:
        raise HTTPException(400, f"Unknown canonical path(s): {bad}")

    cache = CanonicalMapCache()
    if not cache.update_fields(fp, body.updates):
        raise HTTPException(404, f"Mapping '{fp}' not found")
    return _detail(cache, fp)


# ---------------------------------------------------------------------------
# Lock / unlock
# ---------------------------------------------------------------------------

@router.post("/{fp}/lock", summary="Mark a mapping reviewed (locked/authoritative)")
async def lock_mapping(fp: str):
    cache = CanonicalMapCache()
    if not cache.set_reviewed(fp, True):
        raise HTTPException(404, f"Mapping '{fp}' not found")
    return _detail(cache, fp)


@router.post("/{fp}/unlock", summary="Clear the reviewed flag (back to draft)")
async def unlock_mapping(fp: str):
    cache = CanonicalMapCache()
    if not cache.set_reviewed(fp, False):
        raise HTTPException(404, f"Mapping '{fp}' not found")
    return _detail(cache, fp)


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

        label_data = vs._extract_labels_for_fields(str(pdf_path), fields_info)
        field_labels = vs._flatten_field_labels(fields_info, label_data)

        svc = vs._canonical_service
        cache = svc._cache

        # Build (or reuse a locked) canonical map.
        svc.map_fields(fields_info, field_labels)

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

        return _detail(cache, fp)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
