"""
Template Mapping Cache
======================
Caches AI field→value mappings keyed by a fingerprint of the form's
{field_name: inferred_label} dictionary **combined with a hash of the
normalised user data**.

Why include user data in the fingerprint?
  The AI maps *user values* to form fields. Two requests against the same
  template but with different user data (e.g. Alice vs Bob) must produce
  different filled PDFs, so their cache entries must be separate. Without
  user data in the key, the cache would return Alice's values for Bob.

  The user-data hash covers only the *flat* string representation so minor
  structural differences (structured vs flat bundle) do not cause spurious
  misses for identical leaf values.

Why fingerprint labels too?
  The same form may be re-converted by commonforms on every batch run,
  producing slightly different binary output. What stays stable is the set
  of detected field names and the text labels next to them. We hash those.

Cache hit means the AI step is skipped entirely — the same (form × data)
combination is mapped at most once.

Storage layout:
  storage/template_cache/<fingerprint>.json
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings


class MappingEntry:
    """One cached field mapping with confidence score."""

    __slots__ = ("value", "confidence", "source")

    def __init__(self, value: str, confidence: float, source: str = "ai"):
        self.value = value
        self.confidence = float(confidence)
        self.source = source  # "ai" | "cache"

    def to_dict(self) -> dict:
        return {"value": self.value, "confidence": self.confidence, "source": self.source}

    @classmethod
    def from_dict(cls, d: dict) -> "MappingEntry":
        return cls(
            value=str(d.get("value", "")),
            confidence=float(d.get("confidence", 1.0)),
            source=str(d.get("source", "ai")),
        )


class TemplateCache:
    """
    Persist and retrieve AI field mappings (with confidence) for a given
    template fingerprint.

    Usage (inside VisionService):
        cache = TemplateCache()
        fp = cache.fingerprint(field_labels)
        hit = cache.get(fp)
        if hit is None:
            hit = ... call AI ...
            cache.set(fp, hit)
    """

    CACHE_VERSION = 2  # bump when cache schema changes

    @property
    def cache_dir(self) -> Path:
        path = settings.STORAGE_DIR / "template_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Fingerprinting
    # ------------------------------------------------------------------

    @staticmethod
    def fingerprint(
        field_labels: Dict[str, str],
        user_data: "Optional[Dict]" = None,
    ) -> str:
        """
        Stable 32-char hex key from the sorted field-label dict **plus** a
        normalised hash of the user data.

        Including user data ensures that two fill requests for the same
        template with different records get separate cache entries, avoiding
        the value-reuse bug where the first user's values would be returned
        for all subsequent users of the same template.

        Pass ``user_data=None`` (or omit) to get a labels-only fingerprint
        (e.g. for admin cache listing endpoints).
        """
        label_part = json.dumps(
            {k: v for k, v in sorted(field_labels.items())},
            sort_keys=True,
            ensure_ascii=False,
        )
        if user_data:
            # Flatten to a sorted stable string representation.
            # Extract the "flat" sub-dict if this is a canonical bundle.
            flat = user_data.get("flat", user_data) if isinstance(user_data, dict) else user_data
            user_part = json.dumps(
                {k: str(v) for k, v in sorted(flat.items()) if isinstance(k, str)},
                sort_keys=True,
                ensure_ascii=False,
            )
        else:
            user_part = ""
        combined = f"{label_part}\x00{user_part}"
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:32]

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    def _path(self, fp: str) -> Path:
        return self.cache_dir / f"{fp}.json"

    def get(self, fp: str) -> Optional[Dict[str, MappingEntry]]:
        """
        Return cached mappings or None if:
          - file not found
          - schema version mismatch
          - TTL expired (when TEMPLATE_CACHE_TTL_DAYS > 0)
          - TEMPLATE_CACHE_ENABLED is False
        """
        if not settings.TEMPLATE_CACHE_ENABLED:
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

        ttl = settings.TEMPLATE_CACHE_TTL_DAYS
        if ttl and ttl > 0:
            cached_at = datetime.fromisoformat(data["cached_at"])
            if datetime.now() - cached_at > timedelta(days=ttl):
                path.unlink(missing_ok=True)
                return None

        raw_mappings: dict = data.get("mappings", {})
        return {k: MappingEntry.from_dict(v) for k, v in raw_mappings.items()}

    def set(
        self,
        fp: str,
        mappings: Dict[str, MappingEntry],
        field_labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Persist mappings (with confidence) for this fingerprint."""
        if not settings.TEMPLATE_CACHE_ENABLED:
            return
        try:
            self._path(fp).write_text(
                json.dumps(
                    {
                        "version": self.CACHE_VERSION,
                        "cached_at": datetime.now().isoformat(),
                        "fingerprint": fp,
                        "field_labels": field_labels or {},
                        "mappings": {k: v.to_dict() for k, v in mappings.items()},
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"  ⚠️  template cache write failed: {exc}")

    def invalidate(self, fp: str) -> bool:
        """Delete a single cache entry. Returns True if it existed."""
        path = self._path(fp)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_entries(self) -> List[dict]:
        """Summary of all cached templates (for admin / debug routes later)."""
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
