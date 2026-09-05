"""
Template Structure-Signature Cache
==================================
Matching a template to its locked canonical map needs the form's structure
signature, and computing that means opening the fillable PDF and walking every
widget. Doing it inline made one library listing open 160+ PDFs.

The signature is an MD5 over the widgets' ``name:type`` pairs, so it changes
only when the fillable PDF itself changes. That makes it safe to persist and
reuse, keyed on the file's ``(size, mtime_ns)``.

PHI-free: only widget names and types are hashed — never a field value.

Storage:
  {STORAGE_DIR}/template_signature_cache/index.json
      {"<template_id>": {"key": "<size>:<mtime_ns>", "signature": "<md5-12>"}}

A ``signature`` of ``null`` is a real answer ("this PDF has no widgets") and is
cached like any other, so a broken form is not re-opened on every request.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, Iterable, Optional

from ..config import settings

_LOCK = threading.Lock()
_MEM: Dict[str, dict] = {}
_LOADED_FROM: Optional[str] = None


def _index_path() -> Path:
    directory = settings.STORAGE_DIR / "template_signature_cache"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "index.json"


def _load(path: Path) -> None:
    """Populate the in-process map from disk. Caller holds ``_LOCK``."""
    global _LOADED_FROM
    if _LOADED_FROM == str(path):
        return
    data = {}
    try:
        raw = json.loads(path.read_text("utf-8"))
        if isinstance(raw, dict):
            data = {
                k: v for k, v in raw.items()
                if isinstance(v, dict) and isinstance(v.get("key"), str)
            }
    except Exception:
        data = {}
    _MEM.clear()
    _MEM.update(data)
    _LOADED_FROM = str(path)


def _flush(path: Path) -> None:
    """Write the index atomically. Caller holds ``_LOCK``."""
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(_MEM, indent=2, sort_keys=True), "utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # pragma: no cover - best-effort cache
        print(f"  ⚠️  template-signature cache write failed: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _fillable_path(
    template_id: str, service, *, allow_convert: bool
) -> Optional[Path]:
    """Locate the fillable PDF, converting only when explicitly allowed.

    Bulk callers pass ``allow_convert=False``: a CommonForms conversion can take
    seconds, and a form with no fillable version yet cannot be locked anyway.
    """
    repo = getattr(service, "repo", None)
    if repo is not None:
        try:
            existing = repo.get_fillable_path(template_id)
        except Exception:
            existing = None
        if existing is not None:
            return Path(existing)
    if not allow_convert:
        return None
    try:
        return Path(service._ensure_fillable(template_id))
    except Exception:
        return None


def _compute(path: Path) -> Optional[str]:
    from .canonical_map_cache import CanonicalMapCache
    from .vision_service import VisionService

    try:
        fields = VisionService("", "", "")._get_fields_with_coords(str(path))
    except Exception:
        return None
    if not fields:
        return None
    return CanonicalMapCache.signature(fields)


def _stat_key(path: Path) -> Optional[str]:
    try:
        st = path.stat()
    except OSError:
        return None
    return f"{st.st_size}:{st.st_mtime_ns}"


def get_many(
    template_ids: Iterable[str],
    *,
    service=None,
    allow_convert: bool = False,
) -> Dict[str, Optional[str]]:
    """Return ``{template_id: signature or None}``, computing only what changed.

    Loads and writes the index once for the whole batch, so a cold run over the
    full library costs one file write rather than one per template.
    """
    from .template_service import TemplateService

    svc = service or TemplateService()
    ids = list(dict.fromkeys(template_ids))
    out: Dict[str, Optional[str]] = {}
    path = _index_path()
    with _LOCK:
        _load(path)
        dirty = False
        for tid in ids:
            pdf = _fillable_path(tid, svc, allow_convert=allow_convert)
            key = _stat_key(pdf) if pdf is not None else None
            if key is None:
                out[tid] = None
                continue
            hit = _MEM.get(tid)
            if hit is not None and hit.get("key") == key:
                out[tid] = hit.get("signature")
                continue
            signature = _compute(pdf)
            _MEM[tid] = {"key": key, "signature": signature}
            out[tid] = signature
            dirty = True
        if dirty:
            _flush(path)
    return out


def get(
    template_id: str,
    *,
    service=None,
    allow_convert: bool = True,
) -> Optional[str]:
    """Structure signature for one template, or None if it has no widgets."""
    return get_many(
        [template_id], service=service, allow_convert=allow_convert
    ).get(template_id)


def bust(template_id: Optional[str] = None) -> None:
    """Drop cached signatures after a template's PDF is replaced or removed.

    The ``(size, mtime_ns)`` key already catches an overwritten PDF; this is for
    callers that want the entry gone immediately (e.g. template delete).
    """
    path = _index_path()
    with _LOCK:
        _load(path)
        if template_id is None:
            if not _MEM:
                return
            _MEM.clear()
        elif _MEM.pop(template_id, None) is None:
            return
        _flush(path)
