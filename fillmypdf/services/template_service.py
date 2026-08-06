"""
Template Service
=================
Business logic for the Form Template Library.

Key behaviours:
  • list / get / inspect — never call the AI.
  • fill (single record) — reuses VisionService pipeline; fillable PDF is
    lazily generated from commonforms and then *cached on disk* so subsequent
    fills of the same template skip conversion entirely.
  • fill_batch — wraps BatchFillService using the stored template PDF.
  • add / update / delete — admin operations forwarded to TemplateRepository.

Cache strategy for fillable PDFs:
  On first fill: convert template.pdf → fillable.pdf (commonforms), store
  result in the template directory, then use it for the actual fill.
  On every subsequent fill: the fillable.pdf is already there — skip
  conversion and go straight to AI mapping (which also has its own cache).
"""

from __future__ import annotations

import uuid
import shutil
import zipfile
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import settings
from ..models.template import (
    TemplateManifest,
    TemplateListItem,
    TemplateFillResponse,
    TemplateBatchResponse,
)
from ..repositories.template_repository import TemplateRepository
from ..services.pdf_service import PDFService
from ..services.vision_service import VisionService
from ..services.input_adapter import InputAdapter
from ..services.profile_service import ProfileService


class TemplateService:
    """Orchestrate template library operations."""

    def __init__(self) -> None:
        self.repo = TemplateRepository()
        self.pdf_service = PDFService()
        self.input_adapter = InputAdapter()
        self.profile_service = ProfileService()

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def list(
        self,
        *,
        category: Optional[str] = None,
        drug: Optional[str] = None,
        payer: Optional[str] = None,
        state: Optional[str] = None,
        specialty: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[TemplateListItem]:
        return self.repo.list_items(
            category=category,
            drug=drug,
            payer=payer,
            state=state,
            specialty=specialty,
            tag=tag,
            is_public=None,
        )

    def get(self, template_id: str) -> TemplateManifest:
        manifest = self.repo.get(template_id)
        if manifest is None:
            raise KeyError(f"Template '{template_id}' not found")
        return manifest

    def get_pdf_path(self, template_id: str) -> Path:
        self.get(template_id)  # raises KeyError if missing
        pdf = self.repo.get_pdf_path(template_id)
        if pdf is None:
            raise FileNotFoundError(f"Template '{template_id}' has no PDF on disk")
        return pdf

    # ------------------------------------------------------------------
    # Lazy fillable-PDF cache
    # ------------------------------------------------------------------

    def _ensure_fillable(self, template_id: str) -> Path:
        """
        Return path to the fillable (AcroForm) version of the template PDF.
        If it doesn't exist yet, convert now and cache the result.
        """
        if self.repo.has_fillable(template_id):
            return self.repo.get_fillable_path(template_id)  # type: ignore[return-value]

        static_pdf = self.get_pdf_path(template_id)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_out = settings.UPLOAD_DIR / f"{timestamp}_{template_id}_fillable_tmp.pdf"

        ok = self.pdf_service.convert_to_fillable(
            input_path=str(static_pdf),
            output_path=str(tmp_out),
        )
        if not ok:
            raise RuntimeError(f"Could not convert template '{template_id}' to fillable PDF")

        # Store in the template directory for future use
        fillable_bytes = tmp_out.read_bytes()
        tmp_out.unlink(missing_ok=True)
        return self.repo.save_fillable(template_id, fillable_bytes)

    # ------------------------------------------------------------------
    # Inspect fields (no AI)
    # ------------------------------------------------------------------

    def inspect_fields(self, template_id: str) -> Dict[str, Any]:
        """List detected form fields + inferred labels without calling the AI."""
        fillable_path = self._ensure_fillable(template_id)
        vision = VisionService(api_key="-", base_url="https://example.invalid", model="none")
        return vision.inspect_fillable_form(str(fillable_path))

    # ------------------------------------------------------------------
    # Fill — single record
    # ------------------------------------------------------------------

    def _merge_profiles(
        self,
        profile_id: Optional[str],
        profile_ids: Optional[List[str]],
        *,
        owner_id: Optional[str] = None,
        tier: str = "free",
    ) -> dict:
        base: dict = {}
        ids = profile_ids or ([profile_id] if profile_id else [])
        if not ids:
            return base
        try:
            if len(ids) > 1:
                base = self.profile_service.use_profiles(
                    ids, owner_id=owner_id, tier=tier
                )
            else:
                base = self.profile_service.use_profile(
                    ids[0], owner_id=owner_id, tier=tier
                )
        except Exception:
            pass
        return base

    def fill(
        self,
        template_id: str,
        user_data: dict,
        ai_api_key: str,
        ai_base_url: str,
        ai_model: str,
        dpi: int = 200,
        profile_id: Optional[str] = None,
        profile_ids: Optional[List[str]] = None,
        return_mappings: bool = False,
        preserve_input: bool = False,
        owner_id: Optional[str] = None,
        tier: str = "free",
    ) -> TemplateFillResponse:
        """
        Fill one record against the stored template PDF.

        ``preserve_input=True`` bypasses ``InputAdapter`` and feeds ``user_data``
        straight to the pipeline (Guided Fill / Guided Batch canonical keys).
        """
        base = self._merge_profiles(
            profile_id, profile_ids, owner_id=owner_id, tier=tier
        )
        if preserve_input:
            ai_input = {**base, **user_data} if base else dict(user_data)
        else:
            ai_input = self.input_adapter.to_ai_input(user_data, base)

        fillable_path = self._ensure_fillable(template_id)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = uuid.uuid4().hex[:8]
        output_filename = f"{template_id}_{timestamp}_{uid}_filled.pdf"
        output_path = settings.OUTPUT_DIR / output_filename

        vision = VisionService(
            api_key=ai_api_key,
            base_url=ai_base_url,
            model=ai_model,
        )
        result = vision.autofill_pipeline(
            fillable_pdf_path=str(fillable_path),
            output_path=str(output_path),
            user_data=ai_input,
            dpi=dpi,
        )

        return TemplateFillResponse(
            success=result["success"],
            template_id=template_id,
            fields_detected=result.get("fields_detected", 0),
            fields_filled=result.get("fields_filled", 0),
            fields_skipped_low_confidence=result.get("fields_skipped_low_confidence", 0),
            avg_confidence=result.get("avg_confidence"),
            cache_hit=result.get("cache_hit", False),
            canonical_map_reviewed=result.get("canonical_map_reviewed", False),
            download_url=f"/api/v1/templates/download/{output_filename}",
            message=result.get("error"),
            mappings=result.get("mappings") if return_mappings else None,
            confidence=result.get("confidence") if return_mappings else None,
            field_labels=result.get("field_labels") if return_mappings else None,
        )

    # ------------------------------------------------------------------
    # Batch fill — multiple records
    # ------------------------------------------------------------------

    def fill_batch(
        self,
        template_id: str,
        records: List[dict],
        ai_api_key: str,
        ai_base_url: str,
        ai_model: str,
        dpi: int = 200,
        profile_id: Optional[str] = None,
        profile_ids: Optional[List[str]] = None,
        on_record_done: Optional[Callable[[int, int, int], None]] = None,
        *,
        preserve_input: bool = False,
        signature_mode: str = "none",
        consent_given: bool = False,
        signer_name: str = "",
        signer_email: str = "",
        owner_id: Optional[str] = None,
        tier: str = "free",
    ) -> TemplateBatchResponse:
        """Fill N records against the stored template and return a ZIP.

        ``preserve_input=True`` (Guided Batch): records use canonical / ``q:`` /
        ``t:`` keys from the intake CSV template.

        ``signature_mode``:
          * ``none`` — no e-sign overlay
          * ``typed`` — stamp typed names from ``t:<sig_field>`` columns (requires consent)
        """
        from ..services.canonical_map_cache import CanonicalMapCache
        from ..services.esign_service import (
            apply_signature_overlay,
            enrich_signature_placements,
            typed_name_to_png,
        )
        from ..services.form_spec_cache import FormSpecCache
        from ..services.canonical_schema import NARRATIVE_COL_PREFIX

        base = self._merge_profiles(
            profile_id, profile_ids, owner_id=owner_id, tier=tier
        )
        fillable_path = self._ensure_fillable(template_id)
        vision = VisionService(
            api_key=ai_api_key,
            base_url=ai_base_url,
            model=ai_model,
        )

        sig_mode = (signature_mode or "none").strip().lower()
        if sig_mode not in ("none", "typed"):
            sig_mode = "none"
        if sig_mode == "typed" and not consent_given:
            raise ValueError(
                "signature_mode=typed requires consent_given=true (ESIGN / UETA)"
            )

        # Signature placements from FormSpec (once per batch)
        sig_rows: List[dict] = []
        if sig_mode == "typed":
            try:
                fields_info = vision._get_fields_with_coords(str(fillable_path))
                sig = CanonicalMapCache().signature(fields_info)
                spec = FormSpecCache().get(sig)
                raw_sigs = [
                    {
                        "field": s.field,
                        "acro_field": s.acro_field,
                        "label": s.label,
                        "kind": getattr(s, "kind", None) or "signature",
                    }
                    for s in (spec.signatures if spec else [])
                    if (getattr(s, "kind", None) or "signature") != "date"
                ]
                sig_rows = enrich_signature_placements(
                    raw_sigs, fields_info, fillable_path
                )
            except Exception:
                sig_rows = []

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_id = f"tmpl_{uuid.uuid4().hex[:8]}"
        batch_dir = settings.OUTPUT_DIR / f"{timestamp}_{batch_id}_batch"
        batch_dir.mkdir(exist_ok=True)

        results: List[dict] = []
        successful = 0
        failed = 0

        for idx, record in enumerate(records, 1):
            try:
                if preserve_input:
                    ai_input = {**base, **record} if base else dict(record)
                    flat_for_name = ai_input
                else:
                    ai_input = self.input_adapter.to_ai_input(record, base)
                    flat_for_name = ai_input.get("flat", {}) if isinstance(ai_input, dict) else {}
                filename = self._filename_from(flat_for_name, idx)
                # Prefer patient.* keys for guided filenames
                if preserve_input:
                    guided_flat = {
                        "first_name": record.get("patient.first_name") or flat_for_name.get("patient.first_name"),
                        "last_name": record.get("patient.last_name") or flat_for_name.get("patient.last_name"),
                        "full_name": record.get("patient.full_name") or flat_for_name.get("patient.full_name"),
                    }
                    guided_flat = {k: v for k, v in guided_flat.items() if v}
                    if guided_flat:
                        filename = self._filename_from(guided_flat, idx)
                output_path = batch_dir / filename

                result = vision.autofill_pipeline(
                    fillable_pdf_path=str(fillable_path),
                    output_path=str(output_path),
                    user_data=ai_input,
                    dpi=dpi,
                )
                signed = 0
                if result["success"] and sig_mode == "typed" and sig_rows:
                    for s in sig_rows:
                        place = s.get("placement") or {}
                        if not place:
                            continue
                        acro = s.get("acro_field") or s.get("field") or ""
                        text = (
                            record.get(f"{NARRATIVE_COL_PREFIX}{acro}")
                            or record.get(acro)
                            or signer_name
                            or ""
                        )
                        text = str(text).strip()
                        if not text:
                            continue
                        try:
                            png = typed_name_to_png(text)
                            apply_signature_overlay(
                                output_path,
                                output_path,
                                png_bytes=png,
                                page_index=int(place.get("page_index") or 0),
                                x_pct=float(place["x_pct"]),
                                y_pct=float(place["y_pct"]),
                                width_pct=float(place["width_pct"]),
                                height_pct=float(place["height_pct"]),
                                signer_name=signer_name or text,
                                signer_email=signer_email or "",
                                include_timestamp=True,
                            )
                            signed += 1
                        except Exception:
                            continue

                if result["success"]:
                    successful += 1
                else:
                    failed += 1
                results.append({
                    "index": idx,
                    "filename": filename,
                    "success": result["success"],
                    "fields_filled": result.get("fields_filled", 0),
                    "avg_confidence": result.get("avg_confidence"),
                    "cache_hit": result.get("cache_hit", False),
                    "canonical_map_reviewed": result.get("canonical_map_reviewed", False),
                    "signatures_stamped": signed,
                    "error": result.get("error"),
                })
            except Exception as exc:
                failed += 1
                results.append({"index": idx, "success": False, "error": str(exc)})

            if on_record_done is not None:
                on_record_done(idx, successful, failed)

        zip_filename = f"{template_id}_{timestamp}_{batch_id}.zip"
        zip_path = settings.OUTPUT_DIR / zip_filename
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in results:
                if r.get("success") and r.get("filename"):
                    p = batch_dir / r["filename"]
                    if p.exists():
                        zf.write(p, r["filename"])
            zf.writestr(
                "batch_report.json",
                json.dumps(
                    {
                        "batch_id": batch_id,
                        "template_id": template_id,
                        "guided": preserve_input,
                        "signature_mode": sig_mode,
                        "total": len(records),
                        "successful": successful,
                        "failed": failed,
                        "results": results,
                    },
                    indent=2,
                ),
            )

        shutil.rmtree(batch_dir, ignore_errors=True)

        cache_hits = sum(1 for r in results if r.get("cache_hit"))
        conf_vals = [r["avg_confidence"] for r in results if r.get("avg_confidence") is not None]
        overall_conf = round(sum(conf_vals) / len(conf_vals), 3) if conf_vals else None

        return TemplateBatchResponse(
            success=successful > 0,
            template_id=template_id,
            batch_id=batch_id,
            total_records=len(records),
            successful=successful,
            failed=failed,
            success_rate=round(successful / len(records) * 100, 1) if records else 0.0,
            cache_hits=cache_hits,
            avg_confidence=overall_conf,
            download_url=f"/api/v1/templates/download/{zip_filename}",
        )

    # ------------------------------------------------------------------
    # Admin: add / update / delete
    # ------------------------------------------------------------------

    def add(self, manifest: TemplateManifest, pdf_bytes: bytes) -> TemplateManifest:
        if self.repo.exists(manifest.id):
            raise ValueError(f"Template '{manifest.id}' already exists. Use update.")
        return self.repo.save(manifest, pdf_bytes)

    def update_manifest(self, template_id: str, manifest: TemplateManifest) -> TemplateManifest:
        if not self.repo.exists(template_id):
            raise KeyError(f"Template '{template_id}' not found")
        return self.repo.save_manifest_only(manifest)

    def replace_pdf(self, template_id: str, pdf_bytes: bytes) -> None:
        """Replace the template PDF and invalidate the cached fillable."""
        manifest = self.get(template_id)
        self.repo.save(manifest, pdf_bytes)   # save() also invalidates fillable

    def delete(self, template_id: str) -> bool:
        return self.repo.delete(template_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _filename_from(flat: dict, idx: int) -> str:
        parts = []
        for key in ("first_name", "last_name", "full_name", "name"):
            if flat.get(key):
                parts.append(str(flat[key]).replace(" ", "_"))
        base = "_".join(parts) if parts else f"record_{idx}"
        safe = "".join(c for c in base if c.isalnum() or c in ("_", "-"))
        return f"{safe}.pdf"
