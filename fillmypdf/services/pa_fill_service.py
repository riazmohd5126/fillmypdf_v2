"""
pa_fill_service.py
==================
Runtime PA fill: given a blank PDF + a PARequest, produces a filled PDF using
the per-form canonical map built offline.

Fill priority (per field):
  1. ``catalog`` — raw field name resolved deterministically via resolve_label()
  2. ``map``     — stored map from pa_forms.db (name-matched or vision-matched)
  3. ``qwen``    — optional self-hosted Qwen call for remaining unmapped fields
                   (reuses existing ai_provider routing, only when configured)
  4. deferred    — CRITICAL fields with low confidence are left blank and reported

PHI never leaves the local network unless PA_FORCE_LOCAL=False and no local
LLM is configured, in which case unmapped non-critical fields are left blank
rather than sent to a cloud model.

Returns:
  - filled PDF bytes
  - FillReport: per-field provenance + validation results
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pypdf import PdfReader, PdfWriter

from ..models.pa_canonical import (
    CATALOG,
    BY_PATH,
    CRITICAL_FIELDS,
    resolve_label,
    PARequest,
)
from ..services.pa_normalize import normalize, validate
from ..services.pa_map_store import PAMapStore, FormMap, FieldMap, compute_field_signature
from ..config import settings


# ---------------------------------------------------------------------------
# Report structures
# ---------------------------------------------------------------------------

@dataclass
class FieldResult:
    raw_name: str
    canonical_field: Optional[str]
    value: Optional[str]
    normalized: Optional[str]
    valid: bool
    validation_reason: str
    source: str             # catalog | map | qwen | deferred | unmapped
    confidence: str         # high | medium | low | none
    is_critical: bool


@dataclass
class FillReport:
    pdf_filename: str
    total_fields: int
    results: List[FieldResult] = dc_field(default_factory=list)

    @property
    def catalog_count(self) -> int:
        return sum(1 for r in self.results if r.source == "catalog")

    @property
    def map_count(self) -> int:
        return sum(1 for r in self.results if r.source == "map")

    @property
    def qwen_count(self) -> int:
        return sum(1 for r in self.results if r.source == "qwen")

    @property
    def deferred_critical(self) -> int:
        return sum(1 for r in self.results if r.source == "deferred")

    @property
    def unmapped_count(self) -> int:
        return sum(1 for r in self.results if r.source == "unmapped")

    @property
    def filled_count(self) -> int:
        return sum(1 for r in self.results if r.normalized is not None)

    @property
    def validation_failures(self) -> int:
        return sum(1 for r in self.results if not r.valid and r.normalized is not None)

    def summary(self) -> dict:
        return {
            "pdf_filename": self.pdf_filename,
            "total_fields": self.total_fields,
            "filled": self.filled_count,
            "catalog_matched": self.catalog_count,
            "map_matched": self.map_count,
            "qwen_matched": self.qwen_count,
            "deferred_critical": self.deferred_critical,
            "unmapped": self.unmapped_count,
            "validation_failures": self.validation_failures,
            "fill_rate": round(self.filled_count / self.total_fields, 3) if self.total_fields else 0,
        }

    def to_csv_rows(self) -> List[dict]:
        return [
            {
                "raw_name": r.raw_name,
                "canonical_field": r.canonical_field or "",
                "value": r.value or "",
                "normalized": r.normalized or "",
                "valid": r.valid,
                "validation_reason": r.validation_reason,
                "source": r.source,
                "confidence": r.confidence,
                "is_critical": r.is_critical,
            }
            for r in self.results
        ]


# ---------------------------------------------------------------------------
# Value extraction from PARequest
# ---------------------------------------------------------------------------

def _get_value(request: PARequest, dotted_path: str) -> Optional[str]:
    """
    Extract a value from PARequest by dotted canonical path.
    e.g. "patient.dob" -> str(request.patient.dob) or None
    Handles nested Pydantic models + list types (returns first element or str).
    """
    try:
        parts = dotted_path.split(".", 1)
        if len(parts) < 2:
            return None
        entity, attr = parts
        obj = getattr(request, entity, None)
        if obj is None:
            return None
        val = getattr(obj, attr, None)
        if val is None:
            return None
        if isinstance(val, list):
            if not val:
                return None
            # For list fields (prior_therapies, etc.) use the first item as a summary
            item = val[0]
            if hasattr(item, "model_dump"):
                return json.dumps(item.model_dump(exclude_none=True))
            return str(item)
        return str(val)
    except Exception:
        return None


def _field_type_for(canonical_path: str) -> str:
    """Return the semantic type string for a canonical path, default 'text'."""
    f = BY_PATH.get(canonical_path)
    return f.type if f else "text"


# ---------------------------------------------------------------------------
# Core fill logic
# ---------------------------------------------------------------------------

def _write_field(writer: PdfWriter, page_idx: int, field_name: str, value: str) -> bool:
    """Write a value into a named AcroForm field via pypdf."""
    try:
        writer.update_page_form_field_values(
            writer.pages[page_idx], {field_name: value}
        )
        return True
    except Exception:
        return False


def fill_pa_form(
    pdf_bytes: bytes,
    request: PARequest,
    *,
    pdf_filename: str = "form.pdf",
    store: Optional[PAMapStore] = None,
    use_qwen_fallback: bool = True,
    confidence_threshold: str = "low",  # defer criticals below this
) -> Tuple[bytes, FillReport]:
    """
    Fill a PA PDF with values from a PARequest.

    Returns (filled_pdf_bytes, FillReport).
    """
    # Confidence ordering for deferral decisions
    _CONF_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}
    threshold_rank = _CONF_RANK.get(confidence_threshold, 1)

    # --- Read the PDF ---
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            pass

    fields = reader.get_fields() or {}
    report = FillReport(pdf_filename=pdf_filename, total_fields=len(fields))

    if not fields:
        return pdf_bytes, report  # flat/xfa — nothing to fill via AcroForm

    # --- Load the stored map ---
    form_map: Optional[FormMap] = None
    if store is not None:
        try:
            # Try file-path match first (most reliable for known corpus)
            form_map = store.get_by_file(pdf_filename)
            if form_map is None:
                # Compute signature from the PDF bytes and look up
                sig = compute_field_signature(io.BytesIO(pdf_bytes))  # type: ignore[arg-type]
                if sig:
                    form_map = store.get_by_signature(sig)
        except Exception:
            form_map = None

    stored_by_name: Dict[str, FieldMap] = form_map.by_raw_name() if form_map else {}

    # --- Prepare writer ---
    writer = PdfWriter()
    writer.clone_reader_document_root(reader)

    # --- Fill each field ---
    for page_idx, page in enumerate(reader.pages):
        if "/Annots" not in page:
            continue
        for annot in (page.get("/Annots") or []):
            obj = annot.get_object() if hasattr(annot, "get_object") else annot
            if not hasattr(obj, "get"):
                continue
            raw_name = str(obj.get("/T", "")).strip()
            if not raw_name:
                continue

            field_pdf_type = str(obj.get("/FT", "")).strip()
            is_critical = raw_name in CRITICAL_FIELDS  # unlikely by raw_name; checked below

            canonical_path: Optional[str] = None
            confidence = "none"
            source = "unmapped"
            raw_value: Optional[str] = None

            # Step 1: deterministic label resolution via catalog aliases
            catalog_match = resolve_label(raw_name)
            if catalog_match:
                canonical_path = catalog_match
                confidence = "high"
                source = "catalog"

            # Step 2: override/supplement with stored map
            if raw_name in stored_by_name:
                fm = stored_by_name[raw_name]
                if fm.canonical_field != "UNMAPPED":
                    # Use stored map if it's higher confidence than the catalog hit
                    # or if catalog didn't match
                    stored_rank = _CONF_RANK.get(fm.confidence, 0)
                    if canonical_path is None or stored_rank >= _CONF_RANK.get(confidence, 0):
                        canonical_path = fm.canonical_field
                        confidence = fm.confidence
                        source = "map"

            is_critical = canonical_path in CRITICAL_FIELDS if canonical_path else False

            # Step 3: pull value from PARequest
            if canonical_path:
                raw_value = _get_value(request, canonical_path)

            # Step 4: normalize + validate
            field_type = _field_type_for(canonical_path) if canonical_path else "text"
            normed: Optional[str] = None
            valid = True
            validation_reason = ""

            if raw_value is not None:
                normed = normalize(raw_value, field_type)
                valid, validation_reason = validate(normed, field_type)

            # Step 5: critical-field deferral
            if is_critical and _CONF_RANK.get(confidence, 0) < threshold_rank:
                source = "deferred"
                normed = None  # don't write

            # Step 6: Qwen fallback for unmapped non-critical fields
            if source == "unmapped" and use_qwen_fallback and not is_critical:
                qwen_value = _qwen_map_field(raw_name, field_pdf_type, request)
                if qwen_value is not None:
                    canonical_path = None  # no known canonical path
                    raw_value = qwen_value
                    normed = normalize(qwen_value, "text")
                    valid, validation_reason = validate(normed, "text")
                    confidence = "low"
                    source = "qwen"

            # Step 7: write into the PDF
            if normed is not None:
                _write_field(writer, page_idx, raw_name, normed)

            report.results.append(FieldResult(
                raw_name=raw_name,
                canonical_field=canonical_path,
                value=raw_value,
                normalized=normed,
                valid=valid,
                validation_reason=validation_reason,
                source=source,
                confidence=confidence,
                is_critical=is_critical,
            ))

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), report


# ---------------------------------------------------------------------------
# Qwen fallback (best-effort; PHI-safe only when local model is configured)
# ---------------------------------------------------------------------------

def _qwen_map_field(
    raw_name: str,
    field_type: str,
    request: PARequest,
) -> Optional[str]:
    """
    Ask the local LLM to map a raw field name to a value from the PARequest.
    Only fires when a local AI endpoint is configured; skips silently otherwise.
    Returns the raw value string or None.
    """
    try:
        from ..services.ai_provider import resolve_ai_config

        api_key, base_url, model = resolve_ai_config(
            explicit_key=None,
            explicit_url=getattr(settings, "LOCAL_AI_BASE_URL", None),
            explicit_model=getattr(settings, "LOCAL_AI_MODEL", None),
            provider_hint="local",
        )
        if not base_url:
            return None

        import httpx

        patient_summary = _summarize_request(request)
        prompt = (
            f"You are filling a prior authorization form field.\n"
            f"Field name: {raw_name!r}\n"
            f"Field type: {field_type}\n"
            f"Patient/request data:\n{patient_summary}\n\n"
            f"Return ONLY the value to write in this field, or an empty string if unknown."
        )
        resp = httpx.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 64, "temperature": 0},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        return content if content else None
    except Exception:
        return None


def _summarize_request(request: PARequest) -> str:
    """Compact summary of PARequest for the Qwen prompt (no PHI framing needed — local)."""
    lines = []
    data = request.model_dump(exclude_none=True)
    for entity, vals in data.items():
        if isinstance(vals, dict):
            for k, v in vals.items():
                if v:
                    lines.append(f"  {entity}.{k}: {v}")
    return "\n".join(lines[:40])  # cap length
