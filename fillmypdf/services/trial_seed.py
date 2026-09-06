"""Install a small set of locked trial forms into storage.

The packs live under ``fillmypdf/seed/trial`` (inside the Docker image).
Render mounts a disk over ``fillmypdf/storage``, so image-baked templates
would otherwise never appear. This copies them onto the disk on first boot
without overwriting maps or PDFs that are already there.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict, Optional, Union

from ..config import settings

SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "trial"


def install_trial_seed(storage_dir: Optional[Union[str, Path]] = None) -> Dict[str, int]:
    """Copy missing trial templates + locked maps into ``storage_dir``.

    Existing PDFs and map JSON are left untouched. Trial ``manifest.json``
    files are refreshed so display names stay readable.
    """
    copied = {"templates": 0, "manifests": 0, "maps": 0, "skipped": 0}
    root = SEED_DIR
    if not root.is_dir():
        return copied

    storage = Path(storage_dir or settings.STORAGE_DIR)
    tpl_src = root / "templates"
    if tpl_src.is_dir():
        dest_root = storage / "templates"
        dest_root.mkdir(parents=True, exist_ok=True)
        for src in sorted(p for p in tpl_src.iterdir() if p.is_dir()):
            dest = dest_root / src.name
            dest.mkdir(parents=True, exist_ok=True)
            pdf_src = src / "template.pdf"
            pdf_dest = dest / "template.pdf"
            if pdf_src.is_file() and not pdf_dest.is_file():
                shutil.copy2(pdf_src, pdf_dest)
                copied["templates"] += 1
            man_src = src / "manifest.json"
            if man_src.is_file():
                shutil.copy2(man_src, dest / "manifest.json")
                copied["manifests"] += 1

    maps_src = root / "maps"
    if maps_src.is_dir():
        dest_maps = storage / "canonical_map_cache"
        dest_maps.mkdir(parents=True, exist_ok=True)
        for src in sorted(maps_src.glob("*.json")):
            dest = dest_maps / src.name
            if dest.is_file():
                copied["skipped"] += 1
                continue
            shutil.copy2(src, dest)
            copied["maps"] += 1

    return copied


def seed_catalog() -> list:
    """Return the trial catalog (id, name) if present."""
    path = SEED_DIR / "catalog.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return list(data.get("templates") or [])
