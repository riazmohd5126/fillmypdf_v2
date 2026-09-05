"""Who can see a template, and whether it is locked for clinic fill."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from ..models.template import TemplateManifest

LOCK_REQUIRED_MSG = (
    "This form is not locked yet. An admin must lock the mapping in "
    "Mapping Review before it can be filled."
)

FILLED_PDF_MSG = (
    "This PDF already has filled field values. Mapping Review only accepts "
    "blank forms so patient data is not used to build the map."
)


def is_admin(api_key: Optional[dict]) -> bool:
    return (api_key or {}).get("tier") == "admin"


def _visibility(manifest: Any) -> str:
    vis = getattr(manifest, "visibility", None) or (
        "shared" if getattr(manifest, "is_public", True) else "private"
    )
    return vis


def template_visible(manifest: TemplateManifest, api_key: Optional[dict]) -> bool:
    """Shared forms are visible to every authenticated caller.

    Private uploads are limited to the owning API key or the same org_id.
    """
    vis = _visibility(manifest)
    if vis != "private" and getattr(manifest, "is_public", True):
        return True
    if vis != "private":
        return True
    if not api_key:
        return False
    if is_admin(api_key):
        return True
    owner = getattr(manifest, "owner_id", None)
    org = getattr(manifest, "org_id", None)
    if owner and owner == api_key.get("id"):
        return True
    if org and api_key.get("org_id") and org == api_key.get("org_id"):
        return True
    return False


def _norm_map_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: Dict[str, Any] = {"stamp": None, "index": None}


def map_index() -> Dict[str, Set[str]]:
    """Locked-map lookup sets: ``{"signatures": ..., "labels": ...}``.

    Building this reads every file in the canonical-map cache, so it is memoized
    on the cache directory's ``(count, newest mtime)`` stamp — locking a map in
    Mapping Review invalidates it on the next request with no explicit bust.

    Filtering a whole template list must build this **once** and pass it down;
    calling it per template is what made the library listing quadratic.
    """
    from .canonical_map_cache import CanonicalMapCache

    cache = CanonicalMapCache()
    stamp = cache.dir_stamp()
    with _INDEX_LOCK:
        if _INDEX_CACHE["stamp"] == stamp and _INDEX_CACHE["index"] is not None:
            return _INDEX_CACHE["index"]

    sigs: Set[str] = set()
    labels: Set[str] = set()
    for e in cache.list_index():
        if not e.get("reviewed"):
            continue
        sig = (e.get("signature") or "").strip()
        # A locked entry with no mappings dict cannot be honored at fill time.
        if sig and e.get("has_mappings"):
            sigs.add(sig)
        lab = _norm_map_key(e.get("form_label") or "")
        if lab:
            labels.add(lab)
    index = {"signatures": sigs, "labels": labels}
    with _INDEX_LOCK:
        _INDEX_CACHE["stamp"] = stamp
        _INDEX_CACHE["index"] = index
    return index


def reviewed_map_index() -> Tuple[Set[str], Set[str]]:
    """Return (signatures, normalized form_labels) for locked maps."""
    index = map_index()
    return index["signatures"], index["labels"]


def template_has_locked_map(
    template_id: str,
    *,
    service: Any = None,
    index: Optional[Dict[str, Set[str]]] = None,
    allow_convert: bool = True,
) -> bool:
    """True when a reviewed canonical map matches this template's structure.

    The structure signature comes from ``template_signature_cache``, so a warm
    template is answered from a stat + dict lookup instead of a PDF open.
    """
    from .template_service import TemplateService
    from . import template_signature_cache

    svc = service or TemplateService()
    sig = template_signature_cache.get(
        template_id, service=svc, allow_convert=allow_convert
    )
    if not sig:
        return False
    return sig in (index or map_index())["signatures"]


def shared_form_is_ready(
    manifest: Any,
    *,
    service: Any = None,
    index: Optional[Dict[str, Set[str]]] = None,
    allow_convert: bool = True,
) -> bool:
    """Cheap-then-precise check that a shared catalog form has a locked map."""
    index = index or map_index()
    tid = getattr(manifest, "id", "") or ""
    name = getattr(manifest, "name", "") or ""
    labels = index["labels"]
    if _norm_map_key(tid) in labels or _norm_map_key(name) in labels:
        return True
    if not tid:
        return False
    return template_has_locked_map(
        tid, service=service, index=index, allow_convert=allow_convert
    )


def template_listed_for(
    manifest: Any,
    api_key: Optional[dict],
    *,
    service: Any = None,
    index: Optional[Dict[str, Set[str]]] = None,
    allow_convert: bool = True,
) -> bool:
    """Clinics see locked shared forms plus their own private uploads (pending lock).

    Admins see every visible template, including drafts that still need mapping.
    """
    if not template_visible(manifest, api_key):
        return False
    if is_admin(api_key):
        return True
    if _visibility(manifest) == "private":
        return True
    return shared_form_is_ready(
        manifest, service=service, index=index, allow_convert=allow_convert
    )


def acroform_has_filled_values(pdf_path: Path) -> bool:
    """True if any AcroForm widget already holds a non-empty value (possible PHI)."""
    from pypdf import PdfReader

    empty = {None, "", "/Off", "Off", "off"}

    def _nonempty(val) -> bool:
        try:
            if hasattr(val, "get_object"):
                val = val.get_object()
        except Exception:
            pass
        text = str(val).strip() if val is not None else ""
        return bool(text) and text not in empty

    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return False

    fields = reader.get_fields() or {}
    for field in fields.values():
        if isinstance(field, dict) and (
            _nonempty(field.get("/V")) or _nonempty(field.get("/AS"))
        ):
            return True

    for page in reader.pages:
        annots = page.get("/Annots") or []
        try:
            annots = list(annots)
        except TypeError:
            continue
        for annot in annots:
            try:
                obj = annot.get_object() if hasattr(annot, "get_object") else annot
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if str(obj.get("/Subtype") or "") != "/Widget":
                continue
            if _nonempty(obj.get("/V")) or _nonempty(obj.get("/AS")):
                return True
    return False
