"""
Bulk-import PA forms from a source tree into the Template Library and build a
DETERMINISTIC draft canonical map for each (no AI, PHI-free).

Usage:
    PYTHONPATH="$PWD" python scripts/bulk_import_pa.py [LIMIT] [--dirs acroform,flat-digital]

- Imports each PDF as a template (category=prior_authorization). Skips ones that
  already exist.
- Builds the draft canonical map from the SAME fillable the fill path uses
  (_ensure_fillable), so the map signature matches at fill time.
- Deterministic only: CanonicalFieldService with no API key => resolve_label
  pass only, no Gemini. These are review-ready DRAFTS (reviewed=False).
- Local only; does not commit anything.
"""
from __future__ import annotations

import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from fillmypdf.services.template_service import TemplateService
from fillmypdf.services.vision_service import VisionService
from fillmypdf.services.canonical_field_service import CanonicalFieldService
from fillmypdf.models.template import TemplateManifest, TemplatePayer

SRC = Path("/Users/riazmohd/Downloads/test_set")
LOG = Path("bulk_import.log")


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def sanitize(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")
    return (s[:120] or "form")


def main() -> None:
    limit = None
    dirs = ["acroform", "flat-digital"]
    args = sys.argv[1:]
    for a in args:
        if a.startswith("--dirs"):
            dirs = a.split("=", 1)[1].split(",") if "=" in a else dirs
        elif a.isdigit():
            limit = int(a)

    forms: list[tuple[str, Path]] = []
    for d in dirs:
        for p in sorted((SRC / d).rglob("*.pdf")):
            forms.append((d, p))
    if limit:
        forms = forms[:limit]

    svc = TemplateService()
    canon = CanonicalFieldService()  # no key -> deterministic only

    total = len(forms)
    imported = mapped = skipped = errors = 0
    log(f"START {total} forms  dirs={dirs}  limit={limit}")
    t0 = time.time()

    for i, (d, p) in enumerate(forms, 1):
        stem = p.stem
        tid = sanitize(stem)
        payer = p.parent.name
        t_form = time.time()
        try:
            if not svc.repo.exists(tid):
                manifest = TemplateManifest(
                    id=tid,
                    name=stem.replace("_", " ")[:200],
                    category="prior_authorization",
                    payer=TemplatePayer(name=payer),
                    tags=["imported", "test_set", d],
                    custom={"source_engine": d, "source_path": str(p)},
                )
                svc.add(manifest, p.read_bytes())
                imported += 1

            fillable = svc._ensure_fillable(tid)
            vs = VisionService(api_key="-", base_url="", model="")
            fields_info, label_data = vs._inspect_acroform(str(fillable), ai_labels=False)
            if not fields_info:
                skipped += 1
                log(f"[{i}/{total}] {tid}: NO FIELDS ({d}) {time.time()-t_form:.1f}s")
                continue
            labels = VisionService._flatten_field_labels(fields_info, label_data)
            m = canon.map_fields(fields_info, labels)
            nmapped = sum(
                1 for x in m.values()
                if isinstance(x, dict) and x.get("canonical") not in (None, "other")
            )
            mapped += 1
            log(f"[{i}/{total}] {tid}: {len(fields_info)} fields, "
                f"{nmapped} canonical ({d}/{payer}) {time.time()-t_form:.1f}s")
        except Exception as e:
            errors += 1
            log(f"[{i}/{total}] {tid}: ERROR {e}")
            with open(LOG, "a") as fh:
                fh.write(traceback.format_exc() + "\n")

    log(f"DONE imported={imported} mapped={mapped} skipped_nofields={skipped} "
        f"errors={errors} elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
