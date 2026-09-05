"""
Bulk-import PA forms from a source tree into the Template Library and build a
draft canonical map for each (PHI-free; deterministic first, Gemini tail-fill).

Usage:
    PYTHONPATH="$PWD" python scripts/bulk_import_pa.py [LIMIT] [--src PATH] [--dirs acroform,form-flat] [--no-ai]

- Imports each PDF as a template (category=prior_authorization). Skips ones that
  already exist.
- Builds the draft canonical map from the SAME fillable the fill path uses
  (_ensure_fillable), so the map signature matches at fill time.
- Draft building: deterministic ``resolve_label`` first, then the Gemini text
  fallback fills the unmapped tail (blank-form labels only -> PHI-free). This is
  ON by default when a GEMINI_API_KEY is configured; pass ``--no-ai`` or leave
  the key unset for deterministic-only drafts. Either way these are review-ready
  DRAFTS (reviewed=False) — a human still reviews + locks.
- Idempotent: a form that already has a canonical map (matched by structure
  signature) is skipped, so re-running never creates duplicate entries.
- Local only; does not commit anything.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from fillmypdf.config import settings
from fillmypdf.services.template_service import TemplateService
from fillmypdf.services.vision_service import VisionService
from fillmypdf.services.canonical_field_service import CanonicalFieldService
from fillmypdf.services.intake_rules import apply_intake_annotations  # noqa: E402
from fillmypdf.services.form_spec_builder import (
    build_form_spec,
    promote_unresolved_long_text,
)
from fillmypdf.services.form_spec_cache import FormSpecCache
from fillmypdf.models.template import TemplateManifest, TemplatePayer

DEFAULT_SRC = Path("/Users/riazmohd/Downloads/test_set")
LOG = Path("bulk_import.log")


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def sanitize(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")
    return (s[:120] or "form")


def persist_blank(fp: str, pdf_path: Path) -> bool:
    """Copy the fillable PDF so Mapping Review can preview without a rebuild."""
    if not fp or not pdf_path.is_file():
        return False
    dest = settings.STORAGE_DIR / "blank_forms" / f"{fp}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dest.is_file() and dest.stat().st_size > 0:
            return True
        shutil.copy2(pdf_path, dest)
        return True
    except OSError:
        return False


def stamp_import_meta(data: dict, *, tid: str, source: Path, fillable: Path, fp: str) -> dict:
    data["template_id"] = tid
    data["source_path"] = str(source)
    data.setdefault("actor", "import")
    data["has_blank_pdf"] = persist_blank(fp, fillable)
    return data


def main() -> None:
    limit = None
    src = DEFAULT_SRC
    dirs = ["acroform", "flat-digital"]
    use_ai = True
    args = sys.argv[1:]
    for a in args:
        if a.startswith("--src="):
            src = Path(a.split("=", 1)[1]).expanduser()
        elif a.startswith("--dirs="):
            dirs = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]
        elif a == "--no-ai":
            use_ai = False
        elif a.isdigit():
            limit = int(a)

    if not src.is_dir():
        raise SystemExit(f"Source folder not found: {src}")

    forms: list[tuple[str, Path]] = []
    for d in dirs:
        for p in sorted((src / d).rglob("*.pdf")):
            forms.append((d, p))
    if limit:
        forms = forms[:limit]

    svc = TemplateService()

    # Deterministic first, Gemini tail-fill when a key is configured (unless
    # --no-ai). The AI only ever sees blank-form labels, so drafts stay PHI-free.
    key = (settings.GEMINI_API_KEY or "").strip() or os.getenv("GEMINI_API_KEY", "")
    if use_ai and key:
        canon = CanonicalFieldService(key, settings.DEFAULT_AI_BASE_URL, settings.DEFAULT_AI_MODEL)
        ai_state = f"ON ({settings.DEFAULT_AI_MODEL})"
    else:
        canon = CanonicalFieldService()  # deterministic only
        ai_state = "OFF (no key)" if use_ai else "OFF (--no-ai)"

    total = len(forms)
    imported = mapped = skipped = already = errors = 0
    log(f"START {total} forms  dirs={dirs}  limit={limit}  AI tail-fill={ai_state}")
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
            vs = VisionService(
                api_key=key or "-",
                base_url=settings.DEFAULT_AI_BASE_URL,
                model=settings.DEFAULT_AI_MODEL,
            )
            fields_info = vs._get_fields_with_coords(str(fillable))
            if not fields_info:
                skipped += 1
                log(f"[{i}/{total}] {tid}: NO FIELDS ({d}) {time.time()-t_form:.1f}s")
                continue
            # Reuses a cached Gemini label pass when one exists (free), so the
            # form spec gets real question text instead of bare option captions.
            label_data = vs.rich_label_data(str(fillable), fields_info, allow_ai=False)
            labels = VisionService._flatten_field_labels(fields_info, label_data)
            cache = canon._cache
            sig = cache.signature(fields_info)

            spec = build_form_spec(
                fields_info, label_data, signature=sig,
                form_label=stem.replace("_", " ")[:200], widget_key=vs._widget_key,
            )

            # Idempotent: if a map already exists for this blank form (draft or
            # locked), don't rebuild — just ensure it has a friendly label.
            existing = cache.find_by_signature(sig)
            if existing is not None:
                fp_exist = existing.get("fingerprint") or sig
                dirty = False
                if not existing.get("form_label"):
                    existing["form_label"] = stem.replace("_", " ")[:200]
                    dirty = True
                if not existing.get("template_id"):
                    existing["template_id"] = tid
                    dirty = True
                if not existing.get("source_path"):
                    existing["source_path"] = str(p)
                    dirty = True
                if not existing.get("actor"):
                    existing["actor"] = "import"
                    dirty = True
                copied = persist_blank(fp_exist, fillable)
                if copied and not existing.get("has_blank_pdf"):
                    existing["has_blank_pdf"] = True
                    dirty = True
                if dirty:
                    cache.save_full(fp_exist, existing)
                if not FormSpecCache().exists(sig):
                    FormSpecCache().save(spec)
                already += 1
                log(f"[{i}/{total}] {tid}: already mapped (sig={sig}) — skipped "
                    f"{time.time()-t_form:.1f}s")
                continue

            m = canon.map_fields(fields_info, labels, label_data=label_data)
            promote_unresolved_long_text(
                spec, fields_info, label_data,
                canon.unresolved_keys(fields_info, m), widget_key=vs._widget_key,
            )
            m, spec = apply_intake_annotations(
                m, fields_info, label_data, spec, widget_key=vs._widget_key,
            )
            FormSpecCache().save(spec)
            nmapped = sum(
                1 for x in m.values()
                if isinstance(x, dict) and x.get("canonical") not in (None, "other")
            )

            # Stamp a human-friendly form label so Mapping Review shows a name
            # (map_fields stores the draft without one). Use the SAME model the
            # service used so the fingerprint matches the entry just written.
            fp = cache.fingerprint(fields_info, labels, model=canon.model or "")
            data = cache.get_full(fp)
            if data is not None:
                data["mappings"] = m
                if not data.get("form_label"):
                    data["form_label"] = stem.replace("_", " ")[:200]
                data.setdefault("signature", sig)
                stamp_import_meta(data, tid=tid, source=p, fillable=fillable, fp=fp)
                cache.save_full(fp, data)

            mapped += 1
            log(f"[{i}/{total}] {tid}: {len(fields_info)} fields, "
                f"{nmapped} canonical ({d}/{payer}) {time.time()-t_form:.1f}s")
        except Exception as e:
            errors += 1
            log(f"[{i}/{total}] {tid}: ERROR {e}")
            with open(LOG, "a") as fh:
                fh.write(traceback.format_exc() + "\n")

    log(f"DONE imported={imported} mapped={mapped} already_mapped={already} "
        f"skipped_nofields={skipped} errors={errors} elapsed={time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
