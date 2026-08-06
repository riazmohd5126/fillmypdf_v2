#!/usr/bin/env python3
"""
Backfill form specs (and field kinds) for already-imported forms.

Maps built before the canonical/form-spec split have no ``field_kinds`` on the
canonical entry and no form spec at all, so their checkbox questions are
invisible in Mapping Review. This walks the imported templates, records each
widget's kind on the existing canonical entry, and builds the missing spec.

Existing canonical *mappings* are left untouched — including the checkbox rows
those older builds created. Pass ``--prune-choice-maps`` to also drop mappings
on widgets that are no longer canonical-mappable; a locked (reviewed) map is
never modified either way.

    python3 scripts/backfill_form_specs.py [--prune-choice-maps] [--rebuild] [LIMIT]

``--rebuild`` regenerates specs that already exist (needed after a change to the
builder or the label pass). Reviewed specs are never overwritten.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillmypdf.config import settings  # noqa: E402
from fillmypdf.services.canonical_map_cache import CanonicalMapCache  # noqa: E402
from fillmypdf.services.field_classifier import DATA  # noqa: E402
from fillmypdf.services.form_spec_builder import build_form_spec  # noqa: E402
from fillmypdf.services.form_spec_cache import FormSpecCache  # noqa: E402
from fillmypdf.services.intake_rules import (  # noqa: E402
    apply_intake_annotations,
    sync_field_kinds,
)
from fillmypdf.services.template_service import TemplateService  # noqa: E402
from fillmypdf.services.vision_service import VisionService  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    prune = "--prune-choice-maps" in args
    rebuild = "--rebuild" in args
    limit = next((int(a) for a in args if a.isdigit()), None)

    svc = TemplateService()
    cache = CanonicalMapCache()
    specs = FormSpecCache()
    vs = VisionService("", settings.DEFAULT_AI_BASE_URL, settings.DEFAULT_AI_MODEL)

    templates = svc.repo.list_all() if hasattr(svc.repo, "list_all") else svc.list()
    if limit:
        templates = templates[:limit]

    done = skipped = errors = pruned = 0
    total = len(templates)
    for i, t in enumerate(templates, 1):
        tid = t.id if hasattr(t, "id") else t["id"]
        t0 = time.time()
        try:
            fillable = svc._ensure_fillable(tid)
            fields_info = vs._get_fields_with_coords(str(fillable))
            if not fields_info:
                skipped += 1
                continue

            sig = cache.signature(fields_info)
            entry = cache.find_by_signature(sig)
            label_data = vs.rich_label_data(str(fillable), fields_info, allow_ai=False)

            existing_spec = specs.get(sig)
            # Reviewed FormSpecs are never rebuilt (human lock wins).
            if existing_spec is not None and existing_spec.reviewed:
                skipped += 1
                print(f"[{i}/{total}] {tid}: skip reviewed ({time.time()-t0:.1f}s)", flush=True)
                continue

            if rebuild or existing_spec is None:
                spec = build_form_spec(
                    fields_info,
                    label_data,
                    signature=sig,
                    form_label=(entry or {}).get("form_label") or tid,
                    widget_key=vs._widget_key,
                )
            else:
                spec = existing_spec

            if entry is not None and spec is not None:
                if entry.get("reviewed"):
                    # Locked canonical map: upgrade FormSpec only.
                    specs.save(spec)
                else:
                    mappings = entry.get("mappings") or {}
                    before = len(mappings)
                    mappings, spec = apply_intake_annotations(
                        mappings, fields_info, label_data, spec,
                        widget_key=vs._widget_key,
                    )
                    pruned += max(0, before - len(mappings))
                    entry["mappings"] = mappings
                    entry = sync_field_kinds(entry, fields_info)
                    specs.save(spec)
                    cache.save_full(entry.get("fingerprint", sig), entry)
            elif entry is not None and not entry.get("reviewed"):
                entry = sync_field_kinds(entry, fields_info)
                if prune:
                    mappings = entry.get("mappings") or {}
                    kinds = entry.get("field_kinds") or {}
                    drop = [k for k in mappings if kinds.get(k, DATA) != DATA]
                    for k in drop:
                        mappings.pop(k, None)
                    pruned += len(drop)
                    entry["mappings"] = mappings
                cache.save_full(entry.get("fingerprint", sig), entry)
            elif spec is not None:
                specs.save(spec)

            done += 1
            print(f"[{i}/{total}] {tid}: ok ({time.time()-t0:.1f}s)", flush=True)
        except Exception as exc:
            errors += 1
            print(f"[{i}/{total}] {tid}: ERROR {exc}", flush=True)
            traceback.print_exc()

    print(
        f"\ndone={done} skipped={skipped} errors={errors}"
        + (f" pruned_choice_mappings={pruned}" if prune else "")
    )


if __name__ == "__main__":
    main()
