#!/usr/bin/env python3
"""
pa_eval.py — Fill every acroform in the test_set with the golden patient
and produce a coverage / accuracy report.

USAGE:
  # Smoke test (20 forms):
  python3 pa_pipeline/pa_eval.py --limit 20

  # Full acroform run:
  python3 pa_pipeline/pa_eval.py

  # Also score against hand-labeled answer files:
  python3 pa_pipeline/pa_eval.py --answers pa_pipeline/answers/

ANSWER KEY FORMAT (optional):
  pa_pipeline/answers/<formfile_stem>.expected.json
  { "patient.dob": "01/15/1975", "prescriber.npi": "1234567893", ... }

OUTPUTS:
  pa_pipeline/eval_out/
    filled/<payer>/<name>_filled.pdf
    report.csv   (one row per form)
    summary.json (aggregates)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillmypdf.services.pa_fill_service import fill_pa_form, FillReport
from fillmypdf.services.pa_map_store import PAMapStore
from fillmypdf.services.pa_normalize import normalize


DEFAULT_DB   = Path(__file__).parent / "schema_out" / "pa_forms.db"
DEFAULT_TEST = Path("/Users/riazmohd/Downloads/test_set")
DEFAULT_OUT  = Path(__file__).parent / "eval_out"

REPORT_COLS = [
    "file", "payer", "category", "total_fields", "filled",
    "catalog_matched", "map_matched", "qwen_matched",
    "deferred_critical", "unmapped", "validation_failures", "fill_rate",
    "accuracy", "elapsed_s", "error",
]


# ---------------------------------------------------------------------------
# Accuracy scoring (optional, when answer key exists)
# ---------------------------------------------------------------------------

def _score_accuracy(report: FillReport, expected: dict) -> float:
    """Precision: fraction of expected fields that were filled with the right value."""
    if not expected:
        return -1.0
    correct = 0
    for canon_path, exp_val in expected.items():
        for r in report.results:
            if r.canonical_field == canon_path and r.normalized:
                # Compare after normalizing expected too (same type)
                from fillmypdf.models.pa_canonical import BY_PATH
                ft = BY_PATH[canon_path].type if canon_path in BY_PATH else "text"
                norm_exp = normalize(str(exp_val), ft)
                if norm_exp and r.normalized and norm_exp.strip().lower() == r.normalized.strip().lower():
                    correct += 1
                break
    return round(correct / len(expected), 3)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="PA autofill eval harness")
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help="Path to pa_forms.db (default: pa_pipeline/schema_out/pa_forms.db)")
    ap.add_argument("--test-set", default=str(DEFAULT_TEST),
                    help="Path to test_set directory")
    ap.add_argument("--category", default="acroform",
                    choices=["acroform", "flat-digital", "xfa", "all"],
                    help="PDF category to run (default: acroform)")
    ap.add_argument("--payer", default=None,
                    help="Restrict to a specific payer subfolder name")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max forms to fill (0 = all)")
    ap.add_argument("--answers", default=None,
                    help="Directory of <stem>.expected.json answer keys")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="Output directory (default: pa_pipeline/eval_out)")
    ap.add_argument("--no-store", action="store_true",
                    help="Disable pa_forms.db lookup (catalog-only mode)")
    ap.add_argument("--no-qwen", action="store_true",
                    help="Disable Qwen fallback")
    args = ap.parse_args()

    test_root = Path(args.test_set)
    out_root  = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "filled").mkdir(exist_ok=True)

    # Golden patient
    from golden_pa_patient import GOLDEN_PATIENT
    request = GOLDEN_PATIENT

    # Map store
    store: PAMapStore | None = None
    if not args.no_store:
        db_path = Path(args.db)
        if db_path.exists():
            store = PAMapStore(db_path)
            stats = store.stats()
            print(f"pa_forms.db: {stats.get('forms',0)} forms, "
                  f"map_rate={stats.get('map_rate',0):.1%}")
        else:
            print(f"[warn] pa_forms.db not found at {db_path}; running catalog-only")

    # Collect PDFs
    categories = ["acroform", "flat-digital", "xfa"] if args.category == "all" else [args.category]
    pdfs: list[Path] = []
    for cat in categories:
        cat_dir = test_root / cat
        if not cat_dir.exists():
            continue
        if args.payer:
            payer_dir = cat_dir / args.payer
            pdfs.extend(sorted(payer_dir.rglob("*.pdf")))
        else:
            pdfs.extend(sorted(cat_dir.rglob("*.pdf")))

    if args.limit:
        pdfs = pdfs[:args.limit]

    print(f"\nRunning eval on {len(pdfs)} PDFs  (category={args.category})\n{'='*60}")

    # Answer keys
    answers_dir = Path(args.answers) if args.answers else None

    rows = []
    totals: dict = {k: 0 for k in ["total_fields","filled","catalog_matched",
                                    "map_matched","qwen_matched","deferred_critical",
                                    "unmapped","validation_failures"]}
    accuracy_scores = []

    for i, pdf_path in enumerate(pdfs, 1):
        # Derive category + payer from path structure
        parts = pdf_path.relative_to(test_root).parts
        cat   = parts[0] if len(parts) > 1 else "unknown"
        payer = parts[1] if len(parts) > 2 else "unknown"

        # Skip flat-digital and xfa unless explicitly requested
        if cat in ("flat-digital", "xfa") and args.category not in (cat, "all"):
            continue

        row = {c: "" for c in REPORT_COLS}
        row["file"]     = str(pdf_path.relative_to(test_root))
        row["payer"]    = payer
        row["category"] = cat

        t0 = time.perf_counter()
        try:
            pdf_bytes = pdf_path.read_bytes()
            filled_bytes, report = fill_pa_form(
                pdf_bytes,
                request,
                pdf_filename=str(pdf_path),
                store=store,
                use_qwen_fallback=not args.no_qwen,
            )

            # Save filled PDF
            dest_dir = out_root / "filled" / payer
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_pdf = dest_dir / f"{pdf_path.stem}_filled.pdf"
            out_pdf.write_bytes(filled_bytes)

            s = report.summary()
            for k in ["total_fields","filled","catalog_matched","map_matched",
                      "qwen_matched","deferred_critical","unmapped","validation_failures"]:
                row[k] = s.get(k, 0)
                totals[k] += int(s.get(k, 0))
            row["fill_rate"] = s.get("fill_rate", 0)

            # Optional accuracy scoring
            acc = -1.0
            if answers_dir:
                answer_file = answers_dir / f"{pdf_path.stem}.expected.json"
                if answer_file.exists():
                    expected = json.loads(answer_file.read_text())
                    acc = _score_accuracy(report, expected)
                    accuracy_scores.append(acc)
            row["accuracy"] = acc

            elapsed = time.perf_counter() - t0
            row["elapsed_s"] = round(elapsed, 2)

            print(f"  [{i:>3}/{len(pdfs)}] {pdf_path.name[:60]:<60} "
                  f"filled={s['filled']}/{s['total_fields']} "
                  f"cat={s['catalog_matched']} map={s['map_matched']} "
                  f"defer={s['deferred_critical']}")

        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["elapsed_s"] = round(time.perf_counter() - t0, 2)
            print(f"  [{i:>3}/{len(pdfs)}] ERROR {pdf_path.name}: {exc}")

        rows.append(row)

    # Write CSV
    csv_path = out_root / "report.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_COLS)
        w.writeheader()
        w.writerows(rows)

    # Write summary JSON
    n = len(rows)
    errors = sum(1 for r in rows if r["error"])
    summary = {
        "forms_processed": n,
        "errors": errors,
        "category": args.category,
        "totals": totals,
        "avg_fill_rate": round(totals["filled"] / max(totals["total_fields"], 1), 3),
        "avg_accuracy": round(sum(accuracy_scores) / len(accuracy_scores), 3)
                        if accuracy_scores else None,
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2))

    # Console summary
    print(f"\n{'='*60}")
    print(f"EVAL COMPLETE — {n} forms, {errors} errors")
    print(f"  total fields:       {totals['total_fields']}")
    print(f"  filled:             {totals['filled']}  ({summary['avg_fill_rate']:.1%})")
    print(f"  catalog-matched:    {totals['catalog_matched']}")
    print(f"  map-matched:        {totals['map_matched']}")
    print(f"  deferred-critical:  {totals['deferred_critical']}")
    print(f"  unmapped:           {totals['unmapped']}")
    print(f"  validation errors:  {totals['validation_failures']}")
    if accuracy_scores:
        print(f"  accuracy (labeled): {summary['avg_accuracy']:.1%}  ({len(accuracy_scores)} forms scored)")
    print(f"\n  report.csv  -> {csv_path}")
    print(f"  summary.json-> {out_root / 'summary.json'}")
    print(f"  filled PDFs -> {out_root / 'filled'}/")


if __name__ == "__main__":
    main()
