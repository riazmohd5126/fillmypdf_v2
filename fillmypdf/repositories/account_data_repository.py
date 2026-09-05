"""Per-clinic recipes and 'my forms' library (pointers to shared templates)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings


def _safe_id(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (value or "anon"))[:80]


class AccountDataRepository:
    def _root(self, scope_id: str) -> Path:
        path = settings.STORAGE_DIR / "accounts" / _safe_id(scope_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / "recipes").mkdir(exist_ok=True)
        return path

    def library_path(self, scope_id: str) -> Path:
        return self._root(scope_id) / "library.json"

    def recipe_path(self, scope_id: str, fingerprint: str) -> Path:
        return self._root(scope_id) / "recipes" / f"{_safe_id(fingerprint)}.json"

    def get_library(self, scope_id: str) -> dict:
        p = self.library_path(scope_id)
        if not p.exists():
            return {"template_ids": [], "updated_at": None}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {"template_ids": [], "updated_at": None}
        ids = data.get("template_ids") or []
        if not isinstance(ids, list):
            ids = []
        return {
            "template_ids": [str(x) for x in ids if x],
            "updated_at": data.get("updated_at"),
        }

    def save_library(self, scope_id: str, template_ids: List[str]) -> dict:
        seen = set()
        clean: List[str] = []
        for tid in template_ids:
            t = str(tid).strip()
            if not t or t in seen:
                continue
            seen.add(t)
            clean.append(t)
        rec = {
            "template_ids": clean,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.library_path(scope_id).write_text(
            json.dumps(rec, indent=2), encoding="utf-8"
        )
        return rec

    def pin(self, scope_id: str, template_id: str) -> dict:
        lib = self.get_library(scope_id)
        ids = list(lib.get("template_ids") or [])
        if template_id not in ids:
            ids.append(template_id)
        return self.save_library(scope_id, ids)

    def unpin(self, scope_id: str, template_id: str) -> dict:
        lib = self.get_library(scope_id)
        ids = [t for t in (lib.get("template_ids") or []) if t != template_id]
        return self.save_library(scope_id, ids)

    def get_recipe(self, scope_id: str, fingerprint: str) -> Optional[dict]:
        p = self.recipe_path(scope_id, fingerprint)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_recipe(
        self,
        scope_id: str,
        fingerprint: str,
        data: Dict,
        *,
        template_id: Optional[str] = None,
    ) -> dict:
        rec = {
            "fingerprint": fingerprint,
            "template_id": template_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "data": data or {},
        }
        self.recipe_path(scope_id, fingerprint).write_text(
            json.dumps(rec, indent=2, default=str), encoding="utf-8"
        )
        return rec

    def delete_recipe(self, scope_id: str, fingerprint: str) -> bool:
        p = self.recipe_path(scope_id, fingerprint)
        if not p.exists():
            return False
        p.unlink()
        return True

    def list_recipes(self, scope_id: str) -> List[dict]:
        folder = self._root(scope_id) / "recipes"
        rows: List[dict] = []
        for path in folder.glob("*.json"):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        rows.sort(key=lambda r: r.get("saved_at") or "", reverse=True)
        return rows
