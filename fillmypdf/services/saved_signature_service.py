"""
Per-API-key saved signature PNG
===============================
Each API key may store one reusable signature image for e-sign Apply.

Storage:
  STORAGE_DIR/saved_signatures/<api_key_id>/signature.png
  STORAGE_DIR/saved_signatures/<api_key_id>/meta.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import settings

MAX_SAVED_PNG_BYTES = 4_194_304  # 4 MiB


class SavedSignatureService:
    def _dir(self, key_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (key_id or "anon"))[:80]
        path = settings.STORAGE_DIR / "saved_signatures" / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def png_path(self, key_id: str) -> Path:
        return self._dir(key_id) / "signature.png"

    def meta_path(self, key_id: str) -> Path:
        return self._dir(key_id) / "meta.json"

    def get_meta(self, key_id: str) -> Optional[dict]:
        p = self.meta_path(key_id)
        png = self.png_path(key_id)
        if not png.is_file() or not p.is_file():
            return None
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        meta["exists"] = True
        meta["size_bytes"] = png.stat().st_size
        return meta

    def get_png(self, key_id: str) -> Optional[bytes]:
        p = self.png_path(key_id)
        if not p.is_file():
            return None
        return p.read_bytes()

    def save(self, key_id: str, png_bytes: bytes, *, mode: str = "draw", label: str = "") -> dict:
        if not png_bytes:
            raise ValueError("Signature image is empty.")
        if len(png_bytes) > MAX_SAVED_PNG_BYTES:
            raise ValueError("Signature image exceeds 4 MiB.")
        # Basic PNG magic check
        if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("File must be a PNG image.")
        self.png_path(key_id).write_bytes(png_bytes)
        meta = {
            "mode": mode or "draw",
            "label": (label or "My signature")[:80],
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size_bytes": len(png_bytes),
        }
        self.meta_path(key_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    def delete(self, key_id: str) -> bool:
        removed = False
        for p in (self.png_path(key_id), self.meta_path(key_id)):
            if p.is_file():
                p.unlink()
                removed = True
        return removed
