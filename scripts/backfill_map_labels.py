"""
Backfill ``form_label`` on canonical-map drafts (PHI-free, deterministic).

The bulk importer wrote draft maps via ``CanonicalMapCache.set()`` without a
``form_label``, so the Mapping Review list shows bare fingerprints. This script
walks the Template Library, computes each template's stable structure
**signature** (label/model-independent, so it survives label drift), and stamps
``form_label = template.name`` on the matching cache entry.

We key on the signature — not the fingerprint — because the fingerprint mixes in
printed labels + model + schema version, any of which can change between import
and now, orphaning the match. The signature is just the sorted ``name:type``
pairs of the blank form, so it stays put.

Only AcroForm templates are inspected (they already have fields); flat-digital
templates are skipped so we never trigger the heavy commonforms conversion here.
Idempotent: skips entries that already carry a label. Local only; no commits.

Usage:
    PYTHONPATH="$PWD" python scripts/backfill_map_labels.py
"""
from __future__ import annotations

import os
import sys

# Never reach out to Hugging Face for a converter model during a label backfill.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from fillmypdf.services.template_service import TemplateService
from fillmypdf.services.vision_service import VisionService
from fillmypdf.services.canonical_map_cache import CanonicalMapCache


def main() -> None:
    svc = TemplateService()
    cache = CanonicalMapCache()

    items = svc.list()
    print(f"Scanning {len(items)} templates for AcroForm structure signatures…")

    # signature -> template name (first wins; names are per-form anyway).
    sig_to_name: dict[str, str] = {}
    inspected = skipped_flat = no_fields = errors = 0

    for it in items:
        tid = getattr(it, "id", None)
        name = getattr(it, "name", None) or tid
        if not tid:
            continue
        man = svc.repo.get(tid)
        engine = ((man.custom or {}).get("source_engine") if man else None)
        # Skip flat-digital: no AcroForm fields, and converting needs the model.
        if engine == "flat-digital" and not svc.repo.has_fillable(tid):
            skipped_flat += 1
            continue
        try:
            fillable = svc._ensure_fillable(tid)
            vs = VisionService(api_key="-", base_url="", model="")
            fields_info, _ = vs._inspect_acroform(str(fillable), ai_labels=False)
            if not fields_info:
                no_fields += 1
                continue
            sig = cache.signature(fields_info)
            sig_to_name.setdefault(sig, name)
            inspected += 1
        except Exception as exc:
            errors += 1
            print(f"  ERROR {tid}: {exc}", file=sys.stderr)

    # Stamp every unlabeled cache entry whose signature we recognize.
    stamped = already = unknown = 0
    for p in cache.cache_dir.glob("*.json"):
        data = cache.get_full(p.stem)
        if not data:
            continue
        if data.get("form_label"):
            already += 1
            continue
        sig = data.get("signature")
        name = sig_to_name.get(sig)
        if not name:
            unknown += 1
            continue
        data["form_label"] = name
        cache.save_full(p.stem, data)
        stamped += 1
        print(f"  stamped {p.stem[:10]}… -> {name}")

    print(
        f"\nDONE inspected={inspected} skipped_flat={skipped_flat} "
        f"no_fields={no_fields} inspect_errors={errors}\n"
        f"     stamped={stamped} already_labeled={already} "
        f"unmatched_signature={unknown}"
    )


if __name__ == "__main__":
    main()
