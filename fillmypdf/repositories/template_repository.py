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
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import settings
from ..models.template import TemplateManifest, TemplateListItem

# 2-letter codes that are almost always "prior authorization", not Pennsylvania.
_SKIP_STATE_ABBR = {"PA"}

_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "newhampshire": "NH", "newjersey": "NJ", "newmexico": "NM",
    "newyork": "NY", "northcarolina": "NC", "northdakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhodeisland": "RI",
    "southcarolina": "SC", "southdakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "westvirginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "districtofcolumbia": "DC",
}
_STATE_ABBR = set(_STATE_NAMES.values()) | {"DC"}
_NAME_BY_LEN = sorted(_STATE_NAMES.items(), key=lambda kv: len(kv[0]), reverse=True)


def infer_state_from_text(text: str) -> Optional[str]:
    """Pick a US state from a template id/name (e.g. 'Aetna AZ', '...-tx')."""
    raw = text or ""
    if not raw.strip():
        return None
    compact = re.sub(r"[^a-z]+", "", raw.lower())
    for name, abbr in _NAME_BY_LEN:
        if name in compact:
            return abbr
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", raw) if t]
    for tok in tokens:
        if len(tok) != 2:
            continue
        abbr = tok.upper()
        if abbr in _STATE_ABBR and abbr not in _SKIP_STATE_ABBR:
            return abbr
    return None


def resolve_template_state(manifest) -> Optional[str]:
    """payer.state, then custom.state, then id/name inference."""
    payer = getattr(manifest, "payer", None)
    stored = getattr(payer, "state", None) if payer is not None else None
    if stored:
        return str(stored).strip().upper()[:2] or None
    custom = getattr(manifest, "custom", None) or {}
    if isinstance(custom, dict):
        for key in ("state", "us_state"):
            val = custom.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().upper()[:2]
    blob = f"{getattr(manifest, 'id', '') or ''} {getattr(manifest, 'name', '') or ''}"
    return infer_state_from_text(blob)

_LIST_LOCK = threading.Lock()
_LIST_MEMO: Dict[str, Tuple[tuple, List[TemplateManifest]]] = {}


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

    def _manifest_stamp(self, paths: List[Path]) -> tuple:
        """``(count, newest mtime, total size)`` over the manifests — stats only."""
        count = 0
        newest = 0
        total = 0
        for mp in paths:
            try:
                st = mp.stat()
            except OSError:
                continue
            count += 1
            total += st.st_size
            if st.st_mtime_ns > newest:
                newest = st.st_mtime_ns
        return (count, newest, total)

    def list_all(self) -> List[TemplateManifest]:
        """Every manifest on disk, memoized until one of them changes.

        Reading and validating a few hundred manifests on every request is the
        floor cost of the library listing, and it is pure disk I/O — painful on
        a synced volume. The memo is keyed on a stat-only stamp, so an upload or
        edit is picked up immediately.
        """
        root = self.templates_dir
        paths: List[Path] = []
        try:
            with os.scandir(root) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            paths.append(Path(entry.path) / "manifest.json")

        stamp = self._manifest_stamp(paths)
        key = str(root)
        with _LIST_LOCK:
            cached = _LIST_MEMO.get(key)
            if cached and cached[0] == stamp:
                return list(cached[1])

        manifests: List[TemplateManifest] = []
        for mp in paths:
            try:
                manifests.append(TemplateManifest(**json.loads(mp.read_text())))
            except Exception:
                continue

        with _LIST_LOCK:
            _LIST_MEMO[key] = (stamp, manifests)
        return list(manifests)

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
            resolved_state = resolve_template_state(m)
            if state and (resolved_state or "").upper() != state.upper():
                continue

            results.append(TemplateListItem(
                id=m.id,
                name=m.name,
                category=m.category,
                drug_name=m.drug.name if m.drug else None,
                payer_name=m.payer.name if m.payer else None,
                plan_type=m.payer.plan_type if m.payer else None,
                state=resolved_state,
                specialty=m.specialty,
                indications=m.indications,
                tags=m.tags,
                pages=m.pages,
                question_count=len(m.questions),
                field_count=len(m.questions),
                is_public=m.is_public,
                visibility=getattr(m, "visibility", None) or ("shared" if m.is_public else "private"),
                owner_id=getattr(m, "owner_id", None),
                org_id=getattr(m, "org_id", None),
                created_at=m.created_at,
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
