"""
AI-enrich the unmapped tail of existing canonical-map DRAFTS, in place.

The bulk importer built the current drafts deterministically (no Gemini), so
their unmapped tail is empty of AI suggestions. This backfills that tail using
the same text-only Gemini mapper the fill path uses — operating directly on each
stored draft file, so it never changes the fingerprint or creates duplicates.

Safety / scope (mirrors the /api/v1/mappings/{fp}/ai-suggest endpoint):
  - Only fields not already resolved to a real catalog path are sent to Gemini.
  - Manual ('source=manual') and deterministic ('catalog') decisions are kept.
  - The AI sees blank-form labels only -> PHI-free.
  - ``reviewed`` is never flipped: a human still reviews + locks.
  - By default only unreviewed drafts are touched (``--all`` includes locked).

Requires a Gemini key (settings.GEMINI_API_KEY or env GEMINI_API_KEY) and
settings.CANONICAL_AI_FALLBACK enabled.

Usage:
    PYTHONPATH="$PWD" python scripts/ai_enrich_drafts.py [--all] [--limit N]
"""
from __future__ import annotations

import os
import sys

from fillmypdf.config import settings
from fillmypdf.services.canonical_field_service import CanonicalFieldService
from fillmypdf.services.canonical_map_cache import CanonicalMapCache
# Reuse the exact enrichment logic the admin endpoint uses.
from fillmypdf.api.routes.mapping_review_routes import _ai_enrich_one


def main() -> None:
    include_locked = "--all" in sys.argv
    limit = None
    for a in sys.argv[1:]:
        if a.startswith("--limit"):
            v = a.split("=", 1)[1] if "=" in a else ""
            if v.isdigit():
                limit = int(v)

    if not settings.CANONICAL_AI_FALLBACK:
        print("CANONICAL_AI_FALLBACK is disabled — nothing to do.")
        return
    key = (settings.GEMINI_API_KEY or "").strip() or os.getenv("GEMINI_API_KEY", "")
    svc = CanonicalFieldService(key, settings.DEFAULT_AI_BASE_URL, settings.DEFAULT_AI_MODEL)
    if not svc._ai_ready():
        print("No Gemini key configured (GEMINI_API_KEY). Set it and re-run.")
        return

    cache = CanonicalMapCache()
    entries = cache.list_entries()
    targets = [e for e in entries if include_locked or not e.get("reviewed")]
    if limit:
        targets = targets[:limit]

    print(f"Enriching {len(targets)} draft(s) "
          f"(locked included={include_locked}) with {settings.DEFAULT_AI_MODEL}…")

    total_added = touched = errors = 0
    for e in targets:
        fp = e["fingerprint"]
        label = e.get("form_label") or fp[:10]
        try:
            r = _ai_enrich_one(cache, svc, fp)
        except Exception as exc:
            errors += 1
            print(f"  ERROR {label}: {exc}", file=sys.stderr)
            continue
        added = r.get("added", 0)
        total_added += added
        if added:
            touched += 1
            print(f"  +{added:>2} {label}  (of {r.get('candidates', 0)} candidates)")
        elif r.get("error"):
            errors += 1
            print(f"  ERR {label}: {r['error']}", file=sys.stderr)

    print(f"\nDONE forms={len(targets)} enriched={touched} "
          f"new_mappings={total_added} errors={errors}\n"
          f"Review the AI ('source=ai') suggestions in Mapping Review, then lock.")


if __name__ == "__main__":
    main()
