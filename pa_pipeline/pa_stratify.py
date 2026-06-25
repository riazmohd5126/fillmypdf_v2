#!/usr/bin/env python3
"""
pa_stratify.py — turn a profiled PA-form corpus into a minimal, high-coverage
TEST SET.

Two jobs:
  1. DEDUP   — collapse near-identical templates (same field-signature) to one
               representative each. 300 forms -> ~230 distinct templates.
  2. STRATIFY— force-include the rare, high-risk forms regardless of dedup, so
               every axis of the taxonomy is exercised:
                 - all XFA (dynamic forms — distinct fill path)
                 - all forms with comb fields (per-char NPI/DOB cells)
                 - all flat-scanned (OCR path)
                 - all with signature fields
                 - all high-field forms (>= --high-field, default 150)

Output:
  test_set/<structural_type>/<payer>/<file>.pdf   (copied or symlinked)
  test_set_manifest.csv                           (what's included + why)

USAGE:
  python3 pa_stratify.py --profile report/profile.csv --root . --out test_set
  python3 pa_stratify.py ... --symlink        # don't duplicate bytes
  python3 pa_stratify.py ... --dry-run        # manifest only, no file copy
"""

import argparse
import csv
import shutil
import sys
from collections import defaultdict
from pathlib import Path

STRUCT_PRIORITY = {"acroform": 0, "xfa": 1, "flat-digital": 2, "flat-scanned": 3}


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def load(profile_path: Path) -> list[dict]:
    with profile_path.open() as f:
        return list(csv.DictReader(f))


def representative(cluster: list[dict]) -> dict:
    """Richest form in a cluster: most fields, then best structural type."""
    return sorted(
        cluster,
        key=lambda r: (-to_int(r["n_fields"]),
                       STRUCT_PRIORITY.get(r["structural_type"], 9),
                       r["file"]),
    )[0]


def forced_reason(r: dict, high_field: int) -> str | None:
    """Why a form must be kept even if its cluster already has a rep."""
    if r["structural_type"] == "xfa":
        return "xfa"
    if r["structural_type"] == "flat-scanned":
        return "scanned-ocr"
    if to_int(r["n_comb"]) > 0:
        return "comb-field"
    if to_int(r["n_signature"]) > 0:
        return "signature"
    if to_int(r["n_fields"]) >= high_field:
        return "high-field"
    return None


def resolve(path_str: str, root: Path) -> Path | None:
    p = Path(path_str)
    if p.exists():
        return p
    alt = root / path_str
    if alt.exists():
        return alt
    # try stripping a leading "pa_forms/" if root already points inside it
    parts = Path(path_str).parts
    if len(parts) > 1:
        alt2 = root / Path(*parts[1:])
        if alt2.exists():
            return alt2
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="report/profile.csv")
    ap.add_argument("--root", default=".", help="base dir the profile paths resolve against")
    ap.add_argument("--out", default="test_set")
    ap.add_argument("--high-field", type=int, default=150)
    ap.add_argument("--symlink", action="store_true", help="symlink instead of copy")
    ap.add_argument("--dry-run", action="store_true", help="write manifest only, no files")
    args = ap.parse_args()

    profile = Path(args.profile)
    if not profile.exists():
        print(f"!! {profile} not found. Run pa_profiler.py first.")
        sys.exit(1)
    rows = load(profile)
    root = Path(args.root)

    # 1) dedup: one representative per signature cluster
    clusters = defaultdict(list)
    for r in rows:
        clusters[r["field_signature"]].append(r)
    reps = {representative(c)["file"]: "representative" for c in clusters.values()}

    # 2) stratify: force-include rare/high-risk forms
    forced = {}
    for r in rows:
        reason = forced_reason(r, args.high_field)
        if reason:
            forced[r["file"]] = reason

    # union; forced reason wins over plain "representative" for the manifest
    selected = {}
    for r in rows:
        f = r["file"]
        if f in forced:
            selected[f] = (r, forced[f])
        elif f in reps:
            selected[f] = (r, "representative")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = out.parent / "test_set_manifest.csv" if out.name == "test_set" else out / "manifest.csv"

    copied, missing = 0, 0
    with manifest.open("w", newline="") as mf:
        w = csv.writer(mf)
        w.writerow(["file", "payer", "structural_type", "n_fields", "reason", "signature"])
        for f, (r, reason) in sorted(selected.items()):
            w.writerow([f, r["payer"], r["structural_type"], r["n_fields"], reason, r["field_signature"]])
            if args.dry_run:
                continue
            src = resolve(f, root)
            if src is None:
                missing += 1
                continue
            dest_dir = out / r["structural_type"] / r["payer"]
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            try:
                if dest.exists():
                    pass
                elif args.symlink:
                    dest.symlink_to(src.resolve())
                else:
                    shutil.copy2(src, dest)
                copied += 1
            except Exception as e:
                print(f"  [copy err {type(e).__name__}] {f}")

    # ---- report ----
    total = len(rows)
    n_reps = len(reps)
    n_forced_only = sum(1 for f, (_, why) in selected.items()
                        if why != "representative" and f not in reps)
    reasons = defaultdict(int)
    for _, (_, why) in selected.items():
        reasons[why] += 1

    print(f"\n{'='*56}\nSTRATIFIED TEST SET\n{'='*56}")
    print(f"  corpus forms:          {total}")
    print(f"  distinct templates:    {n_reps}")
    print(f"  + forced outliers:     {n_forced_only} (rare mechanics not already a rep)")
    print(f"  = test-set size:       {len(selected)}")
    print(f"\n  selection reasons:")
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"    {k:16} {v}")

    # confirm the test set still covers every mechanic
    sel_rows = [r for r, _ in selected.values()]
    def has(key): return sum(1 for r in sel_rows if to_int(r[key]) > 0)
    print(f"\n  test-set mechanic coverage:")
    for label, key in [("checkbox","n_checkbox"),("radio","n_radio"),("combo","n_combo"),
                       ("signature","n_signature"),("comb","n_comb"),("multiline","n_multiline")]:
        print(f"    {label:10} {has(key)}")
    from collections import Counter
    stc = Counter(r["structural_type"] for r in sel_rows)
    print(f"  structural: " + ", ".join(f"{k}:{v}" for k, v in stc.most_common()))

    if not args.dry_run:
        print(f"\n  copied/linked: {copied}   missing-on-disk: {missing}")
    print(f"  manifest: {manifest}")
    if missing:
        print("  (missing files = profile paths that don't resolve under --root; "
              "point --root at the dir containing pa_forms/)")


if __name__ == "__main__":
    main()
