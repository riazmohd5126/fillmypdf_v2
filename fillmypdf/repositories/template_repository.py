"""
Template Repository
====================
Disk-backed CRUD for form templates.

Directory layout (one sub-folder per template)::

    {STORAGE_DIR}/templates/
        pa_linzess_molina_tx/
            manifest.json
            template.pdf           ← original static PDF
            fillable.pdf           ← cached fillable version (lazily created)
        pa_xifaxan_caremark/
            manifest.json
            template.pdf
        ...

The repository never touches the fillable PDF — that is the job of the service
layer. It only manages manifests and the raw PDF bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings
from ..models.template import TemplateManifest, TemplateListItem


class TemplateRepository:
    """Read/write template manifests and PDF files to disk."""

    # ------------------------------------------------------------------
    # Internal path helpers (properties so monkeypatching works in tests)
    # ------------------------------------------------------------------

    @property
    def templates_dir(self) -> Path:
        path = settings.STORAGE_DIR / "templates"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _template_dir(self, template_id: str) -> Path:
        return self.templates_dir / template_id

    def _manifest_path(self, template_id: str) -> Path:
        return self._template_dir(template_id) / "manifest.json"

    def _pdf_path(self, template_id: str) -> Path:
        return self._template_dir(template_id) / "template.pdf"

    def _fillable_path(self, template_id: str) -> Path:
        """Cached fillable version — may not exist yet."""
        return self._template_dir(template_id) / "fillable.pdf"

    # ------------------------------------------------------------------
    # Existence checks
    # ------------------------------------------------------------------

    def exists(self, template_id: str) -> bool:
        return self._manifest_path(template_id).exists()

    def has_pdf(self, template_id: str) -> bool:
        return self._pdf_path(template_id).exists()

    def has_fillable(self, template_id: str) -> bool:
        return self._fillable_path(template_id).exists()

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list_all(self) -> List[TemplateManifest]:
        manifests: List[TemplateManifest] = []
        for entry in sorted(self.templates_dir.iterdir()):
            if not entry.is_dir():
                continue
            mp = entry / "manifest.json"
            if not mp.exists():
                continue
            try:
                manifests.append(TemplateManifest(**json.loads(mp.read_text())))
            except Exception:
                continue
        return manifests

    def list_items(
        self,
        *,
        category: Optional[str] = None,
        drug: Optional[str] = None,
        payer: Optional[str] = None,
        state: Optional[str] = None,
        specialty: Optional[str] = None,
        tag: Optional[str] = None,
        is_public: Optional[bool] = None,
    ) -> List[TemplateListItem]:
        results: List[TemplateListItem] = []
        for m in self.list_all():
            if category and m.category.lower() != category.lower():
                continue
            if is_public is not None and m.is_public != is_public:
                continue
            if specialty and (not m.specialty or m.specialty.lower() != specialty.lower()):
                continue
            if tag and tag.lower() not in [t.lower() for t in m.tags]:
                continue
            if drug:
                drug_lc = drug.lower()
                drug_match = m.drug and (
                    drug_lc in (m.drug.name or "").lower()
                    or drug_lc in (m.drug.generic_name or "").lower()
                )
                if not drug_match:
                    continue
            if payer:
                payer_lc = payer.lower()
                payer_match = m.payer and payer_lc in (m.payer.name or "").lower()
                if not payer_match:
                    continue
            if state:
                state_match = m.payer and (
                    (m.payer.state or "").upper() == state.upper()
                    or m.payer.state is None
                )
                if not state_match:
                    continue

            results.append(TemplateListItem(
                id=m.id,
                name=m.name,
                category=m.category,
                drug_name=m.drug.name if m.drug else None,
                payer_name=m.payer.name if m.payer else None,
                plan_type=m.payer.plan_type if m.payer else None,
                state=m.payer.state if m.payer else None,
                specialty=m.specialty,
                indications=m.indications,
                tags=m.tags,
                pages=m.pages,
                question_count=len(m.questions),
                is_public=m.is_public,
            ))
        return results

    # ------------------------------------------------------------------
    # Get one
    # ------------------------------------------------------------------

    def get(self, template_id: str) -> Optional[TemplateManifest]:
        mp = self._manifest_path(template_id)
        if not mp.exists():
            return None
        try:
            return TemplateManifest(**json.loads(mp.read_text()))
        except Exception:
            return None

    def get_pdf_path(self, template_id: str) -> Optional[Path]:
        p = self._pdf_path(template_id)
        return p if p.exists() else None

    def get_fillable_path(self, template_id: str) -> Optional[Path]:
        p = self._fillable_path(template_id)
        return p if p.exists() else None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def _write_manifest(self, manifest: TemplateManifest) -> None:
        self._template_dir(manifest.id).mkdir(parents=True, exist_ok=True)
        self._manifest_path(manifest.id).write_text(
            manifest.model_dump_json(indent=2)
        )

    def save(self, manifest: TemplateManifest, pdf_bytes: bytes) -> TemplateManifest:
        """Create or fully replace a template (manifest + PDF)."""
        self._write_manifest(manifest)
        self._pdf_path(manifest.id).write_bytes(pdf_bytes)
        # Invalidate any cached fillable
        fp = self._fillable_path(manifest.id)
        if fp.exists():
            fp.unlink()
        return manifest

    def save_manifest_only(self, manifest: TemplateManifest) -> TemplateManifest:
        """Update manifest without touching the PDF (e.g. edit tags)."""
        if not self._template_dir(manifest.id).exists():
            raise FileNotFoundError(f"Template '{manifest.id}' does not exist")
        self._write_manifest(manifest)
        return manifest

    def save_fillable(self, template_id: str, fillable_bytes: bytes) -> Path:
        """Cache the CommonForms-converted fillable PDF."""
        p = self._fillable_path(template_id)
        p.write_bytes(fillable_bytes)
        return p

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, template_id: str) -> bool:
        import shutil
        d = self._template_dir(template_id)
        if not d.exists():
            return False
        shutil.rmtree(d)
        return True
