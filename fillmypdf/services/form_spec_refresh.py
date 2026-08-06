"""
form_spec_refresh.py
====================
Upgrade stale FormSpec caches after builder changes (e.g. typed signatures).

Reviewed FormSpecs are never overwritten. Callers pass fillable path + fields
when they already have them (Guided Fill); otherwise we resolve a template
whose structure signature matches.
"""

from __future__ import annotations

from typing import Optional, Tuple

from ..models.form_spec import FormSpec
from .canonical_map_cache import CanonicalMapCache
from .form_spec_builder import build_form_spec
from .form_spec_cache import FormSpecCache
from .intake_rules import apply_intake_annotations, sync_field_kinds


def needs_signatures_rebuild(spec: Optional[FormSpec]) -> bool:
    """True when an unlocked FormSpec predates typed /Tx signature detection."""
    if spec is None or getattr(spec, "reviewed", False):
        return False
    return int(getattr(spec, "signatures_version", 0) or 0) < 1


def resolve_fillable_for_signature(
    sig: str,
    *,
    form_label: Optional[str] = None,
) -> Tuple[Optional[str], Optional[list], Optional[str]]:
    """Return ``(fillable_path, fields_info, template_id)`` for ``sig``."""
    from ..config import settings
    from .template_service import TemplateService
    from .vision_service import VisionService

    if not sig:
        return None, None, None

    svc = TemplateService()
    vs = VisionService(
        (settings.GEMINI_API_KEY or "").strip() or "-",
        settings.DEFAULT_AI_BASE_URL,
        settings.DEFAULT_AI_MODEL,
    )
    cache = CanonicalMapCache()

    candidates = []
    if form_label:
        candidates.append(str(form_label).replace(" ", "_"))
        candidates.append(str(form_label))

    templates = svc.repo.list_all() if hasattr(svc.repo, "list_all") else svc.list()
    for t in templates:
        tid = t.id if hasattr(t, "id") else t["id"]
        if tid not in candidates:
            candidates.append(tid)

    seen = set()
    for tid in candidates:
        if not tid or tid in seen:
            continue
        seen.add(tid)
        try:
            fillable = svc._ensure_fillable(str(tid))
            fields_info = vs._get_fields_with_coords(str(fillable))
            if fields_info and cache.signature(fields_info) == sig:
                return str(fillable), fields_info, str(tid)
        except Exception:
            continue
    return None, None, None


def rebuild_form_spec_for_signatures(
    sig: str,
    *,
    form_label: Optional[str] = None,
    fillable_path: Optional[str] = None,
    fields_info: Optional[list] = None,
    entry: Optional[dict] = None,
    widget_key=None,
) -> Optional[FormSpec]:
    """Full-rebuild an unlocked FormSpec and prune signature rows from draft maps.

    Returns the new spec (also saved), or None if rebuild was skipped/failed.
    """
    from ..config import settings
    from .vision_service import VisionService

    specs = FormSpecCache()
    existing = specs.get(sig)
    if not needs_signatures_rebuild(existing):
        return existing

    vs = VisionService(
        (settings.GEMINI_API_KEY or "").strip() or "-",
        settings.DEFAULT_AI_BASE_URL,
        settings.DEFAULT_AI_MODEL,
    )
    if widget_key is None:
        widget_key = vs._widget_key

    if not fields_info or not fillable_path:
        fillable_path, fields_info, tid = resolve_fillable_for_signature(
            sig, form_label=form_label or (existing.form_label if existing else None)
        )
        if tid and not form_label:
            form_label = tid
    if not fields_info or not fillable_path:
        return None

    label_data = vs.rich_label_data(str(fillable_path), fields_info, allow_ai=False)
    rebuilt = build_form_spec(
        fields_info,
        label_data,
        signature=sig,
        form_label=form_label
        or (existing.form_label if existing else None)
        or sig,
        widget_key=widget_key,
    )

    cache = CanonicalMapCache()
    if entry is None:
        entry = cache.find_by_signature(sig)

    if entry is not None and not entry.get("reviewed"):
        mappings = dict(entry.get("mappings") or {})
        mappings, rebuilt = apply_intake_annotations(
            mappings, fields_info, label_data, rebuilt, widget_key=widget_key
        )
        entry = dict(entry)
        entry["mappings"] = mappings
        entry = sync_field_kinds(entry, fields_info)
        cache.save_full(entry.get("fingerprint") or sig, entry)

    specs.save(rebuilt)
    return rebuilt
