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

Scope: **plain data inputs only.** Checkbox/radio widgets, multiline narratives
and signatures are form-specific and handled by ``form_spec_builder`` instead —
see ``field_classifier`` for why (in short: 94% of checkbox→catalog matches were
physically impossible, resolving tick boxes to text-typed paths like
``prescriber.npi``).

Resolution ladder (per field, cheapest → most expensive):
  1. ``resolve_label_conf`` on the printed label → deterministic, free.
       • high  (exact alias)         → accepted as-is.
       • medium (whole-word partial) → accepted, EXCEPT for CRITICAL fields,
         which defer to the AI pass (a wrong critical value can cause a denial).
  2. ``resolve_label_conf`` on the raw field name → same rules as above.
  3. AI text fallback (optional, blank-form labels only) for the remaining
     unresolved / deferred fields → one call per form, cached.

The AI step sees ONLY blank-form labels (schema, never patient values), so it is
PHI-free even on a cloud model. It is gated by ``settings.CANONICAL_AI_FALLBACK``
and only fires when an API key is configured.

Returns ``{map_key: {"canonical": <path|"other">, "confidence": high|medium|low,
"source": catalog|catalog-name|ai, "value"?: <choice>}}``.

``map_key`` is the AcroForm field name, or ``name::export`` for radio-group
options. Optional ``value`` is the catalog choice this widget means when
checked (checkbox/radio → ``field → (path, value)``).
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..config import settings
from ..models.pa_canonical import (
    CATALOG,
    BY_PATH,
    CRITICAL_FIELDS,
    apply_label_role_to_path,
    apply_section_to_path,
    infer_option_value,
    map_field_key,
    map_key_export,
    resolve_label_conf,
)
from .canonical_map_cache import CanonicalMapCache
from .field_classifier import field_kind, is_canonical_candidate


def _label_rec(
    f: dict,
    key: str,
    name: str,
    label_data: Optional[Dict[str, dict]],
) -> dict:
    """Resolve the extract record for a widget.

    Label cache keys are often ``name\\x1f/TU`` (see VisionService._widget_key),
    so a bare AcroForm name miss must fall back to the scoped key.
    """
    if not label_data:
        return {}
    candidates = [
        f.get("_widget_key"),
        key,
        name,
    ]
    tu = f.get("tu")
    if tu:
        candidates.append(f"{name}\x1f{tu}")
    exp = f.get("export_value")
    if exp:
        candidates.append(f"{name}\x1f{exp}")
    for k in candidates:
        if k and k in label_data:
            return label_data[k] or {}
    prefix = f"{name}\x1f"
    for k, v in label_data.items():
        if isinstance(k, str) and k.startswith(prefix) and isinstance(v, dict):
            return v
    return {}


def _section_for(
    f: dict,
    key: str,
    name: str,
    label_data: Optional[Dict[str, dict]],
) -> Optional[str]:
    """Pull extract ``section`` for a widget when rich label_data is available."""
    sec = _label_rec(f, key, name, label_data).get("section")
    return str(sec).strip() if sec else None


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
        label_data: Optional[Dict[str, dict]] = None,
    ) -> Dict[str, dict]:
        """Return ``{field_name: {canonical, confidence, source}}`` for the form.

        Deterministic resolution always runs; the AI fallback (if enabled) only
        covers fields the deterministic pass left unresolved. The merged result
        is cached, so subsequent calls for the same blank form are instant.

        Optional ``label_data`` carries extract ``section`` headings so generic
        labels (Name / Address / Phone) resolve to ``prescriber.*`` when they
        sit under a Prescriber section.

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

        # Whether the Gemini tail-pass will actually run for this form.  Only
        # defer medium-confidence *critical* fields to Gemini when Gemini can
        # in fact answer; otherwise keep the deterministic guess so offline
        # builds still fill those fields.
        ai_available = bool(settings.CANONICAL_AI_FALLBACK and self._ai_ready())

        # ── Deterministic pass (free) ────────────────────────────────────────
        # Tightened matcher returns a confidence tier:
        #   • high   → exact alias match (trusted, accepted as-is)
        #   • medium → whole-word partial match. Accepted for non-critical
        #              fields; for CRITICAL fields we defer to Gemini (a wrong
        #              critical value can cause a denial) when AI is available.
        # Only plain data inputs are offered to the catalog. Checkbox/radio,
        # multiline narratives and signatures are form-specific and live in the
        # FormSpec instead — forcing them through a fixed catalog produced
        # mappings that were 94% impossible (tick boxes resolving to
        # prescriber.npi, medication.drug_name and other text-typed paths).
        candidates = [
            f for f in fields_info if f.get("name") and is_canonical_candidate(f)
        ]
        for f in candidates:
            name = str(f.get("name") or "")
            key = map_field_key(f)
            label = (
                (field_labels.get(key) or field_labels.get(name) or "").strip()
            )
            section = _section_for(f, key, name, label_data)
            # Prefer the human-readable label; fall back to the raw field name.
            path, conf = resolve_label_conf(label) if label else (None, None)
            source = "catalog"
            if not path:
                path, conf = resolve_label_conf(name)
                source = "catalog-name"
            if path:
                remapped = apply_label_role_to_path(path, label, section)
                if remapped and remapped != path:
                    path = remapped
                else:
                    remapped = apply_section_to_path(path, section)
                    if remapped and remapped != path:
                        path = remapped

            # An enum-typed data field (a dropdown, or a text box whose caption
            # names one of the choices) still carries a concrete choice value.
            opt = infer_option_value(path, label) if path else None

            if not path:
                # Stash section for the AI listing.
                if section:
                    f = dict(f)
                    f["_section"] = section
                    f["_map_key"] = key
                unresolved.append(f)
                continue
            if conf == "medium" and path in CRITICAL_FIELDS and ai_available:
                # Risky partial match on a denial-causing field — let Gemini decide.
                if section:
                    f = dict(f)
                    f["_section"] = section
                    f["_map_key"] = key
                unresolved.append(f)
                continue

            entry: Dict[str, str] = {
                "canonical": path,
                "confidence": conf or "medium",
                "source": source,
            }
            if opt:
                entry["value"] = opt
            mapping[key] = entry

        # ── AI fallback for the tail (blank-form labels only → PHI-free) ─────
        if unresolved and settings.CANONICAL_AI_FALLBACK and self._ai_ready():
            try:
                ai_map = self._ai_map(unresolved, field_labels, label_data=label_data)
                for key, entry in ai_map.items():
                    mapping[key] = entry
            except Exception as exc:
                print(f"  ⚠️  canonical AI fallback failed: {exc}")

        field_types = {
            map_field_key(f): str(f.get("type") or "")
            for f in fields_info
            if f.get("name")
        }
        field_kinds = {
            map_field_key(f): field_kind(f) for f in fields_info if f.get("name")
        }
        self._cache.set(
            fp,
            mapping,
            field_labels=field_labels,
            field_types=field_types,
            field_kinds=field_kinds,
            signature=sig,
        )
        mapped = sum(
            1 for m in mapping.values()
            if m.get("canonical") and m.get("canonical") != "other"
        )
        print(
            f"  💾  Canonical-map cache STORED (fp={fp[:8]}…, "
            f"{mapped}/{len(candidates)} data fields mapped, "
            f"{len(fields_info) - len(candidates)} form-specific, no PHI)"
        )
        return mapping

    @staticmethod
    def unresolved_keys(fields_info: List[dict], mapping: Dict[str, dict]) -> set:
        """Canonical-candidate keys the catalog could not place.

        Feeds long-question promotion: a wordy caption only becomes a
        form-specific narrative when the catalog also failed on it.
        """
        out = set()
        for f in fields_info:
            if not f.get("name") or not is_canonical_candidate(f):
                continue
            key = map_field_key(f)
            m = mapping.get(key)
            if not isinstance(m, dict) or m.get("canonical") in (None, "", "other"):
                out.add(key)
        return out

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
            choices = getattr(f, "choices", ()) or ()
            choice_bit = ""
            if choices:
                opts = ", ".join(f"{v}={l}" for v, l in choices[:12])
                if len(choices) > 12:
                    opts += ", …"
                choice_bit = f" choices=[{opts}]"
            lines.append(f"  {f.path} ({f.type}){crit}{choice_bit}")
        return "\n".join(lines)

    def _ai_map(
        self,
        unresolved: List[dict],
        field_labels: Dict[str, str],
        label_data: Optional[Dict[str, dict]] = None,
    ) -> Dict[str, dict]:
        """Ask the model to map unresolved fields → canonical paths (labels only)."""
        from openai import OpenAI

        listing = []
        for f in unresolved:
            name = str(f.get("name") or "")
            # Prefer explicit map key from enrich; else name::export for radios.
            key = str(f.get("_map_key") or "") or (
                map_field_key(f) if name else ""
            ) or name
            section = (
                f.get("_section")
                or _section_for(f, key, name, label_data)
            )
            item = {
                "field": key,
                "label": (
                    field_labels.get(key) or field_labels.get(name) or ""
                ).strip(),
                "type": "checkbox" if "/Btn" in str(f.get("type", "")) else "text",
                "export": f.get("export_value") or map_key_export(key),
            }
            if section:
                item["section"] = section
            listing.append(item)

        prompt = (
            f"{self._schema_prompt()}\n\n"
            "Below is a list of PDF form fields with the printed label next to\n"
            "each (blank form — NO patient data). For EACH field, choose exactly\n"
            "one canonical path from the schema above that the label best matches,\n"
            "or \"other\" if none fits.\n\n"
            "Rules:\n"
            "  - Return ONLY a JSON object keyed by the field id above:\n"
            "      {\"<field>\": {\"canonical\": \"<path or other>\",\n"
            "                    \"confidence\": \"high|medium|low\",\n"
            "                    \"value\": \"<choice value or null>\"}}\n"
            "  - canonical MUST be a path listed above, copied exactly, or \"other\".\n"
            "  - When \"section\" is present, honor it: fields under a Prescriber/\n"
            "    Provider section map to prescriber.* (not patient.*); Patient/\n"
            "    Member sections map to patient.*. Generic labels like Name,\n"
            "    Address, City, Phone are NOT always patient fields.\n"
            "  - For checkbox/radio fields that map to an enum path with choices,\n"
            "    set value to the choice VALUE (left of '=') that this option means\n"
            "    when checked (e.g. Male→M, Physical Therapy→PT). Use null otherwise.\n"
            "  - No markdown, no code fences, no explanation.\n\n"
            f"FIELDS:\n{json.dumps(listing, indent=2)}\n"
        )

        # Bounded timeout + limited retries so a slow/unreachable model endpoint
        # fails fast instead of hanging draft-building (import, /build, ai-suggest).
        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=settings.CANONICAL_AI_TIMEOUT,
            max_retries=1,
        )
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
        # Label/export hints by field key for inferring value when the model omits it.
        hint_by_key = {
            item["field"]: item
            for item in listing
            if item.get("field")
        }
        out: Dict[str, dict] = {}
        for name, v in (parsed or {}).items():
            if not isinstance(name, str):
                continue
            opt_val = None
            if isinstance(v, dict):
                canon = v.get("canonical")
                conf = str(v.get("confidence", "medium")).lower()
                raw_val = v.get("value")
                if raw_val not in (None, "", "null", "None"):
                    opt_val = str(raw_val).strip()
            else:
                canon = str(v)
                conf = "medium"
            # Persist "other" too — Gemini was told to use it when nothing fits.
            # Dropping it left fields looking "unmapped" and re-burned tokens on
            # every AI-suggest pass.
            if canon not in valid_paths and canon != "other":
                continue
            hint = hint_by_key.get(name) or {}
            if canon in valid_paths:
                remapped = apply_label_role_to_path(
                    canon, hint.get("label"), hint.get("section")
                )
                if remapped:
                    canon = remapped
                else:
                    remapped = apply_section_to_path(canon, hint.get("section"))
                    if remapped:
                        canon = remapped
            entry = {
                "canonical": canon,
                "confidence": conf if conf in ("high", "medium", "low") else "medium",
                "source": "ai",
            }
            if canon in valid_paths:
                if not opt_val:
                    opt_val = infer_option_value(
                        canon,
                        hint.get("label"),
                        hint.get("export"),
                        map_key_export(name),
                    )
                # Validate AI-supplied value against catalog choices when present.
                elif BY_PATH[canon].choices:
                    allowed = {str(cv) for cv, _ in BY_PATH[canon].choices}
                    labels = {str(cl).lower(): str(cv)
                              for cv, cl in BY_PATH[canon].choices}
                    if opt_val not in allowed:
                        opt_val = labels.get(opt_val.lower()) or infer_option_value(
                            canon, opt_val, hint.get("label"), hint.get("export")
                        )
                if opt_val:
                    entry["value"] = opt_val
            out[name] = entry
        return out
