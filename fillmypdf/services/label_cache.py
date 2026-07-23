"""
Label Mapping Cache
===================
Caches the FIELD → {label, section, group, ...} mapping produced by the
(expensive) full Gemini vision pass, keyed by a fingerprint of the form's
STRUCTURE only.

Why structure-only (no user data)?
  Unlike ``template_cache.py`` — which caches *value* mappings and therefore
  must mix in the user's data — the label mapping describes the blank form:
  where the fields are and what their printed captions say. That is identical
  for every patient. So the first time we see a form we pay one Gemini call;
  every extraction/fill afterwards reads this cache locally with NO AI call and
  NO PHI leaving the machine.

Fingerprint = sha256 of the sorted list of
  (name, page, x0, y0, x1, y1, type)
tuples from ``VisionService._get_fields_with_coords`` (rounded coords). If the
template PDF changes, the fingerprint changes and the entry is re-computed.

Storage layout:
  storage/label_cache/<fingerprint>.json
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings


class LabelCache:
    """Persist/retrieve structure-keyed label maps (widget_key -> label dict)."""

    CACHE_VERSION = 1  # bump when the cache schema / labeling logic changes

    @property
    def cache_dir(self) -> Path:
        path = settings.STORAGE_DIR / "label_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Fingerprinting
    # ------------------------------------------------------------------
    @staticmethod
    def fingerprint(fields_info: List[dict], *, model: str = "") -> str:
        """
        Stable 32-char hex key from the form's field geometry/structure.

        Includes ``model`` so switching the labeling model invalidates old
        entries (a different model may produce different labels).
        """
        parts = []
        for f in fields_info:
            parts.append((
                str(f.get("qualified_name") or f.get("name") or ""),
                int(f.get("page", 0) or 0),
                int(round(float(f.get("x0", 0) or 0))),
                int(round(float(f.get("y", 0) or 0))),
                int(round(float(f.get("x1", 0) or 0))),
                int(round(float(f.get("y_bottom", 0) or 0))),
                str(f.get("type", "")),
            ))
        parts.sort()
        blob = json.dumps(
            {"model": model, "v": LabelCache.CACHE_VERSION, "fields": parts},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------
    def _path(self, fp: str) -> Path:
        return self.cache_dir / f"{fp}.json"

    def get(self, fp: str) -> Optional[Dict[str, dict]]:
        """Return the cached ``{widget_key: label_dict}`` map or None."""
        if not settings.LABEL_CACHE_ENABLED:
            return None
        path = self._path(fp)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:
            return None
        if data.get("version") != self.CACHE_VERSION:
            return None
        labels = data.get("labels")
        if not isinstance(labels, dict):
            return None
        return labels

    def save(self, fp: str, label_data: Dict[str, dict]) -> None:
        """Persist the label map. Silently no-ops on any write error."""
        if not settings.LABEL_CACHE_ENABLED:
            return
        try:
            payload = {
                "version": self.CACHE_VERSION,
                "labels": label_data,
            }
            self._path(fp).write_text(
                json.dumps(payload, ensure_ascii=False), "utf-8"
            )
        except Exception:
            pass
