"""
canonical_field_service.py
==========================
On-demand "Call 4" — map a form's PDF fields to the FIXED canonical schema
(``pa_canonical.CATALOG``) at request time, and cache the result forever
(PHI-free, keyed by the blank form's structure).

This generalizes the offline ``pa_pipeline/pa_vision_mapper.py`` (which only ran
as a batch job writing ``pa_forms.db``) into a runtime service so an *unseen*
form is understood on first fill and reused instantly thereafter — no offline
build step required.

Resolution ladder (per field, cheapest → most expensive):
  1. ``resolve_label`` on the printed label   → deterministic, free, high conf.
  2. ``resolve_label`` on the raw field name   → deterministic, free, high conf.
  3. AI text fallback (optional, blank-form labels only) for the remaining
     unresolved fields → one call per form, cached.

The AI step sees ONLY blank-form labels (schema, never patient values), so it is
PHI-free even on a cloud model. It is gated by ``settings.CANONICAL_AI_FALLBACK``
and only fires when an API key is configured.

Returns ``{field_name: {"canonical": <path|"other">, "confidence": high|medium|low,
"source": catalog|catalog-name|ai}}``.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..config import settings
from ..models.pa_canonical import CATALOG, BY_PATH, resolve_label
from .canonical_map_cache import CanonicalMapCache


class CanonicalFieldService:
    """Map PDF fields → canonical schema paths, cached per blank form."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or ""
        self.base_url = base_url or ""
        self.model = model or ""
        self._cache = CanonicalMapCache()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def map_fields(
        self,
        fields_info: List[dict],
        field_labels: Dict[str, str],
    ) -> Dict[str, dict]:
        """Return ``{field_name: {canonical, confidence, source}}`` for the form.

        Deterministic resolution always runs; the AI fallback (if enabled) only
        covers fields the deterministic pass left unresolved. The merged result
        is cached, so subsequent calls for the same blank form are instant.

        A human-locked (``reviewed``) map for this form's structure always wins:
        it is returned verbatim (no AI, no re-store), even if labels/model/schema
        changed since it was approved.
        """
        # A reviewed/locked map is authoritative — match it by stable structure
        # signature so a re-label or schema bump can't orphan it.
        sig = self._cache.signature(fields_info)
        locked = self._cache.get_by_signature(sig)
        if locked is not None:
            print(f"  🔒  Canonical-map REVIEWED hit (sig={sig}) — locked, no AI")
            return locked.get("mappings", {})

        fp = self._cache.fingerprint(fields_info, field_labels, model=self.model or "")
        cached = self._cache.get(fp)
        if cached is not None:
            print(f"  ✅  Canonical-map cache HIT (fp={fp[:8]}…) — no AI call")
            return cached

        mapping: Dict[str, dict] = {}
        unresolved: List[dict] = []

        # ── Deterministic pass (free, high confidence) ───────────────────────
        for f in fields_info:
            name = str(f.get("name") or "")
            if not name:
                continue
            label = (field_labels.get(name) or "").strip()
            # Prefer the human-readable label; fall back to the raw field name.
            path = resolve_label(label) if label else None
            source = "catalog"
            if not path:
                path = resolve_label(name)
                source = "catalog-name"
            if path:
                mapping[name] = {
                    "canonical": path,
                    "confidence": "high",
                    "source": source,
                }
            else:
                unresolved.append(f)

        # ── AI fallback for the tail (blank-form labels only → PHI-free) ─────
        if unresolved and settings.CANONICAL_AI_FALLBACK and self._ai_ready():
            try:
                ai_map = self._ai_map(unresolved, field_labels)
                for name, entry in ai_map.items():
                    mapping[name] = entry
            except Exception as exc:
                print(f"  ⚠️  canonical AI fallback failed: {exc}")

        self._cache.set(fp, mapping, field_labels=field_labels, signature=sig)
        mapped = sum(
            1 for m in mapping.values()
            if m.get("canonical") and m.get("canonical") != "other"
        )
        print(
            f"  💾  Canonical-map cache STORED (fp={fp[:8]}…, "
            f"{mapped}/{len(fields_info)} fields mapped, no PHI)"
        )
        return mapping

    def is_reviewed(self, fields_info: List[dict]) -> bool:
        """True if a human-locked (reviewed) canonical map exists for this form."""
        try:
            sig = self._cache.signature(fields_info)
            return self._cache.get_by_signature(sig) is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # AI fallback (text-only; blank-form labels; PHI-free)
    # ------------------------------------------------------------------
    def _ai_ready(self) -> bool:
        return bool(self.api_key) and self.api_key not in ("-", "") and bool(self.base_url)

    @staticmethod
    def _schema_prompt() -> str:
        lines = [
            "You map PDF form fields to a FIXED canonical schema for prior-",
            "authorization / medical forms. Canonical paths (path — type):",
        ]
        for f in CATALOG:
            crit = " *critical*" if getattr(f, "required", False) else ""
            lines.append(f"  {f.path} ({f.type}){crit}")
        return "\n".join(lines)

    def _ai_map(
        self,
        unresolved: List[dict],
        field_labels: Dict[str, str],
    ) -> Dict[str, dict]:
        """Ask the model to map unresolved fields → canonical paths (labels only)."""
        from openai import OpenAI

        listing = [
            {
                "field": str(f.get("name") or ""),
                "label": (field_labels.get(str(f.get("name") or "")) or "").strip(),
                "type": "checkbox" if "/Btn" in str(f.get("type", "")) else "text",
            }
            for f in unresolved
        ]

        prompt = (
            f"{self._schema_prompt()}\n\n"
            "Below is a list of PDF form fields with the printed label next to\n"
            "each (blank form — NO patient data). For EACH field, choose exactly\n"
            "one canonical path from the schema above that the label best matches,\n"
            "or \"other\" if none fits.\n\n"
            "Rules:\n"
            "  - Return ONLY a JSON object:\n"
            "      {\"<field>\": {\"canonical\": \"<path or other>\",\n"
            "                    \"confidence\": \"high|medium|low\"}}\n"
            "  - canonical MUST be a path listed above, copied exactly, or \"other\".\n"
            "  - No markdown, no code fences, no explanation.\n\n"
            f"FIELDS:\n{json.dumps(listing, indent=2)}\n"
        )

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Return strict JSON only. No markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            print(f"  ⚠️  canonical AI returned non-JSON: {raw[:200]}")
            return {}

        valid_paths = set(BY_PATH.keys())
        out: Dict[str, dict] = {}
        for name, v in (parsed or {}).items():
            if not isinstance(name, str):
                continue
            if isinstance(v, dict):
                canon = v.get("canonical")
                conf = str(v.get("confidence", "medium")).lower()
            else:
                canon = str(v)
                conf = "medium"
            if canon in valid_paths:
                out[name] = {
                    "canonical": canon,
                    "confidence": conf if conf in ("high", "medium", "low") else "medium",
                    "source": "ai",
                }
        return out
