"""
Signature Detect Cache
======================
Caches Detect results (AcroForm / AI / heuristic zones) keyed by PDF content
fingerprint so Gemini is not re-run for the same blank form.

Storage: ``STORAGE_DIR/signature_detect_cache/<fingerprint>.json``

Only **successful** detections (one or more zones) are cached. Empty misses are
not stored, so a transient AI failure does not stick forever.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from ..config import settings


class SignatureDetectCache:
    CACHE_VERSION = 2

    @property
    def cache_dir(self) -> Path:
        path = settings.STORAGE_DIR / "signature_detect_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def fingerprint(
        pdf_bytes: bytes,
        *,
        use_ai: bool,
        model: str = "",
        max_pages: int = 3,
    ) -> str:
        h = hashlib.sha256()
        h.update(b"sigdetect|")
        h.update(str(SignatureDetectCache.CACHE_VERSION).encode())
        h.update(b"|ai=" + (b"1" if use_ai else b"0"))
        h.update(b"|model=" + (model or "").encode())
        h.update(b"|pages=" + str(max_pages).encode())
        h.update(b"|")
        h.update(pdf_bytes)
        return h.hexdigest()[:32]

    def _path(self, fp: str) -> Path:
        return self.cache_dir / f"{fp}.json"

    def get(self, fp: str) -> Optional[dict[str, Any]]:
        p = self._path(fp)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("cache_version") != self.CACHE_VERSION:
            return None
        fields = data.get("fields")
        if not isinstance(fields, list) or not fields:
            return None
        return data

    def save(self, fp: str, *, fields: list, meta: dict, source: str, message: str) -> None:
        if not fields:
            return
        payload = {
            "cache_version": self.CACHE_VERSION,
            "fingerprint": fp,
            "fields": fields,
            "meta": meta,
            "source": source,
            "message": message,
        }
        self._path(fp).write_text(json.dumps(payload, indent=2), encoding="utf-8")
