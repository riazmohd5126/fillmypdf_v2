"""
Field-Mapping Cache (PHI-free)
==============================
Caches the SCHEMA mapping produced by the AI — i.e. *which user-data key feeds
which PDF field* — keyed by a fingerprint of the form's ``{field_name: label}``
dict **combined with the sorted list of user-data KEYS** (never the values).

Why keys, never values?
  The mapping "user key ``patient_name`` → PDF field ``2``" depends only on the
  form's labels and the *shape* of the incoming data (its key names), not on the
  actual patient values. So:

    * No PHI is ever written to disk (only field names, labels, and key names —
      all schema, not patient data).
    * On a cache hit the AI is skipped and the fill is applied locally.
    * Because values are never part of the request, PHI never reaches the AI at
      all — on a hit OR a miss (a miss sends labels + key names only).

Contrast with the deprecated ``template_cache.py`` which stored the *filled
values* (PHI) keyed on the values themselves.

Fingerprint = sha256 of
  { model, version, labels: {field: label}, user_keys: [sorted key names] }

Storage layout:
  storage/field_map_cache/<fingerprint>.json
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings


class FieldMapCache:
    """Persist/retrieve PHI-free field→source-key mappings."""

    CACHE_VERSION = 1  # bump when the schema / mapping logic changes

    @property
    def cache_dir(self) -> Path:
        path = settings.STORAGE_DIR / "field_map_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Fingerprinting
    # ------------------------------------------------------------------
    @staticmethod
    def fingerprint(
        field_labels: Dict[str, str],
        user_keys: List[str],
        *,
        model: str = "",
    ) -> str:
        """Stable 32-char hex key from labels + sorted user KEYS (no values)."""
        label_part = {k: v for k, v in sorted(field_labels.items())}
        key_part = sorted(str(k) for k in user_keys)
        blob = json.dumps(
            {
                "model": model,
                "v": FieldMapCache.CACHE_VERSION,
                "labels": label_part,
                "keys": key_part,
            },
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
        """Return the cached ``{pdf_field: {source_key, confidence}}`` map or None."""
        if not settings.FIELD_MAP_CACHE_ENABLED:
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
        mappings = data.get("mappings")
        if not isinstance(mappings, dict):
            return None
        return mappings

    def set(
        self,
        fp: str,
        mappings: Dict[str, dict],
        *,
        field_labels: Optional[Dict[str, str]] = None,
        user_keys: Optional[List[str]] = None,
    ) -> None:
        """Persist the schema mapping. Silently no-ops on any write error.

        ``mappings`` must contain ONLY schema data, e.g.
        ``{"2": {"source_key": "patient_name", "confidence": 0.95}}`` — never a
        patient value.
        """
        if not settings.FIELD_MAP_CACHE_ENABLED:
            return
        try:
            payload = {
                "version": self.CACHE_VERSION,
                "cached_at": datetime.now().isoformat(),
                "fingerprint": fp,
                "field_labels": field_labels or {},
                "user_keys": sorted(str(k) for k in (user_keys or [])),
                "mappings": mappings,
            }
            self._path(fp).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), "utf-8"
            )
        except Exception as exc:  # pragma: no cover - best-effort cache
            print(f"  ⚠️  field-map cache write failed: {exc}")

    def invalidate(self, fp: str) -> bool:
        """Delete a single cache entry. Returns True if it existed."""
        path = self._path(fp)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_entries(self) -> List[dict]:
        """Summary of all cached mappings (for admin / debug routes)."""
        entries = []
        for p in self.cache_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text("utf-8"))
                entries.append(
                    {
                        "fingerprint": data.get("fingerprint", p.stem),
                        "cached_at": data.get("cached_at"),
                        "field_count": len(data.get("mappings", {})),
                    }
                )
            except Exception:
                continue
        return sorted(entries, key=lambda e: e.get("cached_at") or "", reverse=True)
