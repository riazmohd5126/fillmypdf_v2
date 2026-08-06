"""
form_spec_cache.py
==================
Persist one :class:`~fillmypdf.models.form_spec.FormSpec` per blank form.

Keyed by the form's **structure signature** — the same MD5 of ``name:type``
pairs the canonical map uses — rather than a label-based fingerprint. A form
spec describes the document's own wording, so re-labelling or a catalog change
must not orphan it.

Deliberately a separate store from ``canonical_map_cache``: the two halves
version independently, so improving a form spec never invalidates a canonical
map a human already reviewed and locked.

PHI-free: blank-form question text and structure only.

Storage layout:
  storage/form_spec_cache/<signature>.json
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from ..config import settings
from ..models.form_spec import FormSpec


class FormSpecCache:
    """Persist/retrieve per-form question, narrative and signature specs."""

    CACHE_VERSION = 1

    @property
    def cache_dir(self) -> Path:
        path = settings.STORAGE_DIR / "form_spec_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _path(self, signature: str) -> Path:
        return self.cache_dir / f"{signature}.json"

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get(self, signature: str) -> Optional[FormSpec]:
        if not signature:
            return None
        path = self._path(signature)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            return FormSpec.model_validate(data.get("spec") or data)
        except Exception:
            return None

    def exists(self, signature: str) -> bool:
        return bool(signature) and self._path(signature).exists()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def save(self, spec: FormSpec) -> bool:
        """Write a spec. A reviewed spec is never silently overwritten."""
        if not spec.signature:
            return False
        existing = self.get(spec.signature)
        if existing is not None and existing.reviewed and not spec.reviewed:
            return False
        payload = {
            "version": self.CACHE_VERSION,
            "updated_at": datetime.now().isoformat(),
            "spec": spec.model_dump(mode="json"),
        }
        try:
            self._path(spec.signature).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), "utf-8"
            )
            return True
        except Exception as exc:  # pragma: no cover - best-effort cache
            print(f"  ⚠️  form-spec cache write failed: {exc}")
            return False

    def set_reviewed(self, signature: str, reviewed: bool) -> bool:
        spec = self.get(signature)
        if spec is None:
            return False
        spec.reviewed = bool(reviewed)
        # Bypass the overwrite guard: this call is the reviewer's own decision.
        payload = {
            "version": self.CACHE_VERSION,
            "updated_at": datetime.now().isoformat(),
            "spec": spec.model_dump(mode="json"),
        }
        try:
            self._path(signature).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), "utf-8"
            )
            return True
        except Exception:
            return False

    def update_question(
        self,
        signature: str,
        question_id: str,
        *,
        question: Optional[str] = None,
        canonical_hint: Optional[str] = None,
        input_type: Optional[str] = None,
    ) -> bool:
        """Apply a reviewer's correction to one question group."""
        spec = self.get(signature)
        if spec is None:
            return False
        for q in spec.questions:
            if q.id != question_id:
                continue
            if question is not None:
                q.question = question
            if input_type in ("radio", "checkbox"):
                q.input = input_type
            if canonical_hint is not None:
                q.canonical_hint = canonical_hint or None
            return self._force_save(spec)
        return False

    def merge_questions(
        self,
        signature: str,
        question_ids: List[str],
        *,
        question: Optional[str] = None,
        input_type: Optional[str] = None,
    ) -> Optional[FormSpec]:
        """Merge several question cards into one. Returns the updated spec or None."""
        from .form_spec_builder import merge_question_groups

        spec = self.get(signature)
        if spec is None or len(question_ids) < 2:
            return None
        want = list(dict.fromkeys(question_ids))  # preserve order, unique
        picked = [q for q in spec.questions if q.id in want]
        if len(picked) != len(want):
            return None
        taken = {q.id for q in spec.questions}
        merged = merge_question_groups(
            picked, question=question, input_type=input_type, taken=taken
        )
        drop = set(want)
        keep = [q for q in spec.questions if q.id not in drop]
        keep.append(merged)
        keep.sort(key=lambda q: q.order)
        spec.questions = keep
        if not self._force_save(spec):
            return None
        return spec

    def recluster_solos(self, signature: str) -> Optional[FormSpec]:
        """Re-run solo clustering on an existing spec (fixes Q===option duplicates)."""
        from .form_spec_builder import cluster_solo_questions

        spec = self.get(signature)
        if spec is None:
            return None
        taken = {q.id for q in spec.questions}
        spec.questions = cluster_solo_questions(sorted(spec.questions, key=lambda q: q.order), taken)
        if not self._force_save(spec):
            return None
        return spec

    def set_signature_role(self, signature: str, field: str, role: str) -> bool:
        spec = self.get(signature)
        if spec is None:
            return False
        for s in spec.signatures:
            if s.field == field:
                s.role = role or None
                return self._force_save(spec)
        return False

    def _force_save(self, spec: FormSpec) -> bool:
        payload = {
            "version": self.CACHE_VERSION,
            "updated_at": datetime.now().isoformat(),
            "spec": spec.model_dump(mode="json"),
        }
        try:
            self._path(spec.signature).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), "utf-8"
            )
            return True
        except Exception:
            return False

    def invalidate(self, signature: str) -> bool:
        path = self._path(signature)
        if path.exists():
            path.unlink()
            return True
        return False

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------
    def list_entries(self) -> List[dict]:
        out = []
        for p in self.cache_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text("utf-8"))
                spec = FormSpec.model_validate(data.get("spec") or data)
            except Exception:
                continue
            out.append(
                {
                    "signature": spec.signature,
                    "form_label": spec.form_label,
                    "updated_at": data.get("updated_at"),
                    "reviewed": spec.reviewed,
                    "question_count": len(spec.questions),
                    "option_count": spec.option_count,
                    "long_text_count": len(spec.long_text),
                    "signature_count": len(spec.signatures),
                }
            )
        return sorted(out, key=lambda e: e.get("updated_at") or "", reverse=True)
