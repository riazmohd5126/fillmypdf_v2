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

    def fill(
        self,
        template_id: str,
        user_data: dict,
        ai_api_key: str,
        ai_base_url: str,
        ai_model: str,
        dpi: int = 200,
        profile_id: Optional[str] = None,
    ) -> TemplateFillResponse:
        """
        Fill one record against the stored template PDF.

        Returns a TemplateFillResponse; the filled PDF is written to
        OUTPUT_DIR and the download_url field points to it.
        """
        # 1. Merge optional profile data
        base: dict = {}
        if profile_id:
            try:
                base = self.profile_service.use_profile(profile_id)
            except ValueError:
                pass

        # 2. Normalise input through InputAdapter
        ai_input = self.input_adapter.to_ai_input(user_data, base)

        # 3. Ensure we have a fillable PDF (cached after first call)
        fillable_path = self._ensure_fillable(template_id)

        # 4. Build output path
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = uuid.uuid4().hex[:8]
        output_filename = f"{template_id}_{timestamp}_{uid}_filled.pdf"
        output_path = settings.OUTPUT_DIR / output_filename

        # 5. Run AI pipeline
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
            download_url=f"/api/v1/templates/download/{output_filename}",
            message=result.get("error"),
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
        on_record_done: Optional[Callable[[int, int, int], None]] = None,
    ) -> TemplateBatchResponse:
        """Fill N records against the stored template and return a ZIP."""
        # Merge optional profile base
        base: dict = {}
        if profile_id:
            try:
                base = self.profile_service.use_profile(profile_id)
            except ValueError:
                pass

        fillable_path = self._ensure_fillable(template_id)
        vision = VisionService(
            api_key=ai_api_key,
            base_url=ai_base_url,
            model=ai_model,
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_id = f"tmpl_{uuid.uuid4().hex[:8]}"
        batch_dir = settings.OUTPUT_DIR / f"{timestamp}_{batch_id}_batch"
        batch_dir.mkdir(exist_ok=True)

        results: List[dict] = []
        successful = 0
        failed = 0

        for idx, record in enumerate(records, 1):
            try:
                ai_input = self.input_adapter.to_ai_input(record, base)
                filename = self._filename_from(ai_input.get("flat", {}), idx)
                output_path = batch_dir / filename

                result = vision.autofill_pipeline(
                    fillable_pdf_path=str(fillable_path),
                    output_path=str(output_path),
                    user_data=ai_input,
                    dpi=dpi,
                )
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
                    "error": result.get("error"),
                })
            except Exception as exc:
                failed += 1
                results.append({"index": idx, "success": False, "error": str(exc)})

            if on_record_done is not None:
                on_record_done(idx, successful, failed)

        # Build ZIP
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
                    {"batch_id": batch_id, "template_id": template_id,
                     "total": len(records), "successful": successful,
                     "failed": failed, "results": results},
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
