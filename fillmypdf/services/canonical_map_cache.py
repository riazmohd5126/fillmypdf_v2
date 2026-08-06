"""
Canonical-Mapping Cache (PHI-free)
==================================
Caches the **field → canonical-path** mapping produced by "Call 4" (canonical
resolution: deterministic ``resolve_label`` + optional AI fallback), keyed by a
fingerprint of the blank form's structure (field names + labels + types) plus
the model and the canonical-schema version.

Why this is PHI-free:
  The mapping "PDF field ``2`` → ``patient.dob``" describes the *blank form*
  only — which caption sits next to which widget. It is identical for every
  patient, so:
    * No patient values are ever part of the key or the stored payload.
    * The (optional) AI fallback that builds it is shown blank-form labels only.
    * On a cache hit the whole canonical pass is skipped and reused locally.

Review / lock:
  Each entry carries a ``reviewed`` flag and a stable ``signature`` (an MD5 of
  the form's ``name:type`` pairs, independent of labels/model/schema). Once an
  admin edits + locks a map (``reviewed=true``) it becomes authoritative:
    * It is honored verbatim by the fill pipeline (looked up by ``signature``),
    * It is never overwritten by the AI, and
    * It survives ``schema_version`` / model / label changes (the label-based
      fingerprint would otherwise orphan it).

Contrast:
  * ``label_cache.py``      caches field → {label, section, group} (geometry/vision).
  * ``field_map_cache.py``  caches field → user-data KEY (Call 3, general fork).
  * ``canonical_map_cache`` caches field → CANONICAL PATH (Call 4, canonical fork).

Fingerprint = sha256 of
  { model, cache_version, schema_version, fields: [(name, label, type), ...] }

Storage layout:
  storage/canonical_map_cache/<fingerprint>.json
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings

try:
    from ..models.pa_canonical import CATALOG, BY_PATH, CRITICAL_FIELDS
    # A stable hash of the canonical schema so that editing CATALOG (adding a
    # field, renaming a path) automatically invalidates every *unreviewed*
    # cached mapping.
    _SCHEMA_VERSION = hashlib.sha256(
        ",".join(sorted(f.path for f in CATALOG)).encode("utf-8")
    ).hexdigest()[:12]
except Exception:  # pragma: no cover - schema import should always succeed
    _SCHEMA_VERSION = "0"
    BY_PATH = {}
    CRITICAL_FIELDS = set()


class CanonicalMapCache:
    """Persist/retrieve PHI-free field → canonical-path mappings."""

    CACHE_VERSION = 1  # bump when the stored payload shape changes

    @property
    def cache_dir(self) -> Path:
        path = settings.STORAGE_DIR / "canonical_map_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Fingerprinting / signatures
    # ------------------------------------------------------------------
    @staticmethod
    def fingerprint(
        fields_info: List[dict],
        field_labels: Dict[str, str],
        *,
        model: str = "",
    ) -> str:
        """Stable 32-char hex key from (map_key, label, type) triples + schema ver.

        ``map_key`` is ``name`` or ``name::export`` for radio options so each
        option's label contributes to the fingerprint.
        """
        from ..models.pa_canonical import map_field_key

        parts = []
        for f in fields_info:
            name = str(f.get("name") or "")
            key = map_field_key(f) if name else ""
            parts.append((
                key,
                str(field_labels.get(key) or field_labels.get(name, "") or ""),
                str(f.get("type", "") or ""),
            ))
        parts.sort()
        blob = json.dumps(
            {
                "model": model,
                "v": CanonicalMapCache.CACHE_VERSION,
                "schema": _SCHEMA_VERSION,
                "fields": parts,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def signature(fields_info: List[dict]) -> str:
        """Stable 12-char structure key: MD5 of sorted ``name:type`` pairs.

        Independent of labels, model and schema version — mirrors
        ``pa_map_store.compute_field_signature`` so a locked map keeps matching
        the same blank form even after a re-label or schema bump.
        """
        parts = sorted(
            f"{f.get('name', '')}:{f.get('type', '')}" for f in fields_info
        )
        blob = "FORM:" + ";".join(parts)
        return hashlib.md5(blob.encode("utf-8")).hexdigest()[:12]

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def _path(self, fp: str) -> Path:
        return self.cache_dir / f"{fp}.json"

    def _read_payload(self, path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def get(self, fp: str) -> Optional[Dict[str, dict]]:
        """Return the cached ``{field_name: {canonical, confidence, source}}`` map.

        Reviewed (locked) entries are returned even when the current
        ``schema_version`` differs — a human-approved map is authoritative.
        Unreviewed drafts are dropped on version/schema mismatch so a stale
        auto-guess never lingers after the schema changes.
        """
        if not settings.CANONICAL_MAP_CACHE_ENABLED:
            return None
        data = self._read_payload(self._path(fp))
        if data is None:
            return None
        reviewed = bool(data.get("reviewed", False))
        if data.get("version") != self.CACHE_VERSION and not reviewed:
            return None
        if data.get("schema_version") != _SCHEMA_VERSION and not reviewed:
            return None
        mappings = data.get("mappings")
        if not isinstance(mappings, dict):
            return None
        return mappings

    def get_full(self, fp: str) -> Optional[dict]:
        """Return the full stored payload (mappings, labels, reviewed, …) or None."""
        return self._read_payload(self._path(fp))

    def get_by_signature(self, sig: str) -> Optional[dict]:
        """Return a **reviewed** entry whose structure signature matches ``sig``.

        Used by the fill pipeline to honor a locked map regardless of the
        label-based fingerprint. Returns the full payload (includes ``mappings``
        and ``fingerprint``) or None when no locked map matches.
        """
        if not settings.CANONICAL_MAP_CACHE_ENABLED or not sig:
            return None
        for p in self.cache_dir.glob("*.json"):
            data = self._read_payload(p)
            if not data:
                continue
            if data.get("signature") == sig and bool(data.get("reviewed", False)):
                if isinstance(data.get("mappings"), dict):
                    return data
        return None

    def find_by_signature(self, sig: str) -> Optional[dict]:
        """Return ANY entry (draft or reviewed) whose structure signature matches.

        Unlike :meth:`get_by_signature` (reviewed-only, used at fill time), this
        is for import/build code that just needs to know whether a map already
        exists for this blank form — regardless of the label-based fingerprint or
        the model used — so it can avoid creating a duplicate entry.
        """
        if not settings.CANONICAL_MAP_CACHE_ENABLED or not sig:
            return None
        for p in self.cache_dir.glob("*.json"):
            data = self._read_payload(p)
            if data and data.get("signature") == sig:
                return data
        return None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def set(
        self,
        fp: str,
        mappings: Dict[str, dict],
        *,
        field_labels: Optional[Dict[str, str]] = None,
        field_types: Optional[Dict[str, str]] = None,
        field_kinds: Optional[Dict[str, str]] = None,
        signature: Optional[str] = None,
        form_label: Optional[str] = None,
        reviewed: bool = False,
    ) -> None:
        """Persist the canonical mapping. Silently no-ops on any write error.

        ``mappings`` holds ONLY schema data, e.g.
        ``{"2": {"canonical": "patient.dob", "confidence": "high",
        "source": "catalog"}}`` — never a patient value.

        ``field_types`` is optional AcroForm type per field (e.g. ``/Btn``)
        so AI-suggest can tell Gemini which widgets are checkboxes.

        ``field_kinds`` records each widget's bucket (data / choice / longtext /
        signature). Only ``data`` fields are canonical-mappable, so coverage
        stats and the review UI use this to avoid counting a checkbox as an
        unmapped canonical field.
        """
        if not settings.CANONICAL_MAP_CACHE_ENABLED:
            return
        payload = {
            "version": self.CACHE_VERSION,
            "schema_version": _SCHEMA_VERSION,
            "cached_at": datetime.now().isoformat(),
            "fingerprint": fp,
            "signature": signature,
            "form_label": form_label,
            "reviewed": reviewed,
            "field_labels": field_labels or {},
            "field_types": field_types or {},
            "field_kinds": field_kinds or {},
            "mappings": mappings,
        }
        self.save_full(fp, payload)

    def save_full(self, fp: str, payload: dict) -> bool:
        """Write an arbitrary payload dict, normalizing version/timestamp fields."""
        if not settings.CANONICAL_MAP_CACHE_ENABLED:
            return False
        try:
            payload = dict(payload)
            payload["version"] = self.CACHE_VERSION
            payload.setdefault("schema_version", _SCHEMA_VERSION)
            payload["fingerprint"] = fp
            payload["updated_at"] = datetime.now().isoformat()
            payload.setdefault("cached_at", payload["updated_at"])
            self._path(fp).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), "utf-8"
            )
            return True
        except Exception as exc:  # pragma: no cover - best-effort cache
            print(f"  ⚠️  canonical-map cache write failed: {exc}")
            return False

    def update_fields(self, fp: str, updates: Dict[str, object]) -> bool:
        """Apply admin corrections ``{field: canonical|"other"|""|{canonical,value}}``.

        Setting a field to "" (or None) removes its mapping. A string sets the
        canonical path (clears any prior option ``value``). A dict may include
        ``canonical`` and optional ``value`` (checkbox/radio choice). Stored as
        source='manual', confidence='high'.
        """
        data = self.get_full(fp)
        if data is None:
            return False
        mappings = data.get("mappings")
        if not isinstance(mappings, dict):
            mappings = {}
        for field, spec in updates.items():
            if spec in (None, ""):
                mappings.pop(field, None)
                continue
            opt_val = None
            if isinstance(spec, dict):
                canonical = spec.get("canonical")
                raw_val = spec.get("value")
                if raw_val not in (None, ""):
                    opt_val = str(raw_val)
            else:
                canonical = spec
            if canonical in (None, ""):
                mappings.pop(field, None)
                continue
            entry = {
                "canonical": str(canonical),
                "confidence": "high",
                "source": "manual",
            }
            if opt_val is not None:
                entry["value"] = opt_val
            mappings[field] = entry
        data["mappings"] = mappings
        return self.save_full(fp, data)

    def set_reviewed(self, fp: str, reviewed: bool) -> bool:
        """Toggle the locked/approved flag on an entry."""
        data = self.get_full(fp)
        if data is None:
            return False
        data["reviewed"] = bool(reviewed)
        return self.save_full(fp, data)

    def invalidate(self, fp: str) -> bool:
        """Delete a single cache entry. Returns True if it existed."""
        path = self._path(fp)
        if path.exists():
            path.unlink()
            return True
        return False

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------
    def list_entries(self) -> List[dict]:
        """Summary of all cached canonical mappings (for admin / review routes).

        Coverage stats let reviewers triage worst-first:
          * ``total_fields``      — every widget on the form (labeled + mapped).
          * ``mapped_count``      — widgets resolved to a real catalog path.
          * ``unmapped_count``    — widgets still on ``other``/unmapped.
          * ``critical_unmapped`` — CRITICAL canonical paths this form does NOT
                                    yet cover (a denial risk if left blank).
        """
        entries = []
        for p in self.cache_dir.glob("*.json"):
            data = self._read_payload(p)
            if not data:
                continue
            mappings = data.get("mappings", {}) or {}
            labels = data.get("field_labels", {}) or {}
            kinds = data.get("field_kinds", {}) or {}
            names = set(labels.keys()) | set(mappings.keys())
            # Coverage is about canonical-mappable fields only. Checkboxes,
            # narratives and signatures live in the form spec, so counting them
            # here would report a form as mostly unmapped when it is complete.
            form_specific = 0
            if kinds:
                form_specific = sum(
                    1 for n in names if kinds.get(n, "data") != "data"
                )
                names = {n for n in names if kinds.get(n, "data") == "data"}
            mapped_paths = {
                m["canonical"] for m in mappings.values()
                if isinstance(m, dict) and m.get("canonical") in BY_PATH
            }
            mapped_count = sum(
                1 for m in mappings.values()
                if isinstance(m, dict) and m.get("canonical") in BY_PATH
            )
            other_count = sum(
                1 for m in mappings.values()
                if isinstance(m, dict) and m.get("canonical") == "other"
            )
            total = len(names)
            # "other" is an AI/manual decision ("nothing fits"), not pending work.
            unmapped_count = max(total - mapped_count - other_count, 0)
            entries.append(
                {
                    "fingerprint": data.get("fingerprint", p.stem),
                    "signature": data.get("signature"),
                    "form_label": data.get("form_label"),
                    "cached_at": data.get("cached_at"),
                    "updated_at": data.get("updated_at"),
                    "reviewed": bool(data.get("reviewed", False)),
                    "field_count": total,
                    "total_fields": total,
                    "form_specific_count": form_specific,
                    "mapped_count": mapped_count,
                    "other_count": other_count,
                    "unmapped_count": unmapped_count,
                    "critical_unmapped": len(set(CRITICAL_FIELDS) - mapped_paths),
                }
            )
        return sorted(entries, key=lambda e: e.get("updated_at") or e.get("cached_at") or "", reverse=True)
