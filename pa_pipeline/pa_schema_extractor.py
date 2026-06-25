#!/usr/bin/env python3
"""
pa_schema_extractor.py — derive the canonical PA field model from a corpus.

Walks your harvested forms, pulls every raw AcroForm field name, clusters
them into canonical concepts (member_id, npi, dob, ...), and emits:

  canonical_schema.json   the canonical field definitions (your data model)
  field_alias_map.csv     raw_name -> canonical_field, confidence, frequency
                          (UNMAPPED rows = your review queue)
  pa_forms.db             SQLite: canonical_fields, forms, form_fields tables

Design note: the canonical SCHEMA lives in code (here) — it's structure, not
data. The per-form MAPPINGS go in the DB — that's relational data that grows.

USAGE:
  pip install "pypdf[crypto]"
  python3 pa_schema_extractor.py --root pa_forms --out schema_out

Then REVIEW field_alias_map.csv: confirm the auto-clustering, fix UNMAPPED and
any medium-confidence rows (PA-specific ambiguities need your domain eye), and
re-load. The alias map you confirm here is ALSO what powers autofill mapping.
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from pypdf import PdfReader

# ---------------------------------------------------------------------------
# CANONICAL FIELD MODEL — derived from the unified pa_canonical.py catalog.
#
# We import from fillmypdf.models.pa_canonical when the repo is on the path,
# and fall back to a local import of canonical_model.py (same file, placed
# alongside this script) so pa_schema_extractor.py works standalone too.
#
# The local CANON tuple format: (canonical_key, entity, critical, high_patterns, low_patterns)
# where canonical_key uses the dotted path from CATALOG (e.g. "patient.dob").
# ---------------------------------------------------------------------------

def _build_canon_from_catalog():
    """Derive CANON from the unified CATALOG in pa_canonical / canonical_model."""
    catalog = None
    # Try repo import first
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from fillmypdf.models.pa_canonical import CATALOG
        catalog = CATALOG
    except ImportError:
        pass

    if catalog is None:
        # Fallback: standalone — import canonical_model.py next to this script
        script_dir = Path(__file__).resolve().parent
        if str(script_dir) not in sys.path:
            sys.path.insert(0, str(script_dir))
        try:
            from canonical_model import CATALOG
            catalog = CATALOG
        except ImportError:
            catalog = None

    if catalog is None:
        raise RuntimeError(
            "Cannot import CATALOG. Ensure fillmypdf/models/pa_canonical.py exists "
            "or canonical_model.py is alongside this script."
        )

    canon = []
    for f in catalog:
        # entity = first segment of dotted path (patient, insurance, prescriber, …)
        entity = f.path.split(".")[0]
        # Translate aliases (printed label strings) into name-pattern tuples.
        # All aliases become high-confidence patterns; the dotted key itself is a low fallback.
        high = list(f.aliases)
        low = [f.path.split(".")[-1].replace("_", " ")]  # leaf name as low-tier fallback
        canon.append((f.path, entity, f.required, high, low))
    return canon


CANON = _build_canon_from_catalog()


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(name).lower())).strip()


def _hit(pattern: str, norm: str) -> bool:
    return re.search(r"\b" + re.escape(pattern) + r"\b", norm) is not None


def classify_field(raw: str):
    """Return (canonical_field, confidence). High tier wins globally."""
    norm = normalize(raw)
    if not norm:
        return ("UNMAPPED", "none")
    for cname, _e, _c, high, _low in CANON:
        if any(_hit(p, norm) for p in high):
            return (cname, "high")
    for cname, _e, _c, _high, low in CANON:
        if any(_hit(p, norm) for p in low):
            return (cname, "low")
    return ("UNMAPPED", "none")


def field_names(reader: PdfReader):
    out = []
    try:
        fields = reader.get_fields() or {}
    except Exception:
        return out
    for name, f in fields.items():
        ft = f.get("/FT") if hasattr(f, "get") else None
        out.append((str(name), str(ft)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="pa_forms")
    ap.add_argument("--out", default="schema_out")
    args = ap.parse_args()

    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(root.rglob("*.pdf"))
    if not pdfs:
        print(f"!! no PDFs under {root}")
        return

    # ---- 1. emit canonical schema (code -> JSON) ----
    schema = [{"canonical_field": c, "entity": e, "critical": cr,
               "example_aliases": (high[:3])} for c, e, cr, high, low in CANON]
    (out / "canonical_schema.json").write_text(json.dumps(schema, indent=2))

    # ---- 2. walk forms, classify every raw field ----
    db = sqlite3.connect(out / "pa_forms.db")
    db.executescript("""
        DROP TABLE IF EXISTS canonical_fields;
        DROP TABLE IF EXISTS forms;
        DROP TABLE IF EXISTS form_fields;
        CREATE TABLE canonical_fields(name TEXT PRIMARY KEY, entity TEXT, critical INT);
        CREATE TABLE forms(form_id INTEGER PRIMARY KEY, file TEXT, payer TEXT, n_fields INT);
        CREATE TABLE form_fields(form_id INT, raw_name TEXT, field_type TEXT,
                                 canonical_field TEXT, confidence TEXT);
    """)
    db.executemany("INSERT INTO canonical_fields VALUES(?,?,?)",
                   [(c, e, int(cr)) for c, e, cr, _h, _l in CANON])

    alias_counter = Counter()           # (raw_norm, canonical, conf) -> freq
    alias_example = {}                  # raw_norm -> original raw name
    canon_form_count = defaultdict(set)  # canonical -> set(form_id) for frequency
    fid = 0
    profiled = 0
    for p in pdfs:
        reader_fields = []
        try:
            reader = PdfReader(str(p), strict=False)
            if reader.is_encrypted:
                try: reader.decrypt("")
                except Exception: pass
            reader_fields = field_names(reader)
        except Exception:
            continue
        if not reader_fields:
            continue                    # flat/scanned: no AcroForm names to mine
        fid += 1
        profiled += 1
        db.execute("INSERT INTO forms VALUES(?,?,?,?)",
                   (fid, str(p), p.parent.name, len(reader_fields)))
        for raw, ft in reader_fields:
            canon, conf = classify_field(raw)
            db.execute("INSERT INTO form_fields VALUES(?,?,?,?,?)",
                       (fid, raw, ft, canon, conf))
            nrm = normalize(raw)
            alias_counter[(nrm, canon, conf)] += 1
            alias_example.setdefault(nrm, raw)
            if canon != "UNMAPPED":
                canon_form_count[canon].add(fid)
    db.commit()

    # ---- 3. alias map CSV (the review artifact) ----
    with (out / "field_alias_map.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["raw_field_name", "normalized", "canonical_field", "confidence", "frequency"])
        for (nrm, canon, conf), freq in alias_counter.most_common():
            w.writerow([alias_example.get(nrm, nrm), nrm, canon, conf, freq])

    # ---- 4. report ----
    total_raw = sum(alias_counter.values())
    unmapped = sum(f for (n, c, cf), f in alias_counter.items() if c == "UNMAPPED")
    low = sum(f for (n, c, cf), f in alias_counter.items() if cf == "low")
    print(f"\n{'='*58}\nCANONICAL SCHEMA EXTRACTION\n{'='*58}")
    print(f"  forms with AcroForm fields mined: {profiled}/{len(pdfs)}")
    print(f"  raw field instances classified:   {total_raw}")
    print(f"  distinct raw names:                {len(alias_example)}")
    print(f"  canonical fields defined:          {len(CANON)}")
    print(f"  UNMAPPED instances (review queue): {unmapped}")
    print(f"  low-confidence (review):           {low}")
    print(f"\n  Canonical field frequency (how many forms request each):")
    ranked = sorted(canon_form_count.items(), key=lambda x: -len(x[1]))
    for canon, forms in ranked:
        crit = next((c for c, e, cr, h, l in CANON if c == canon and cr), None)
        mark = " *CRITICAL" if crit else ""
        print(f"    {canon:22} {len(forms):4} forms{mark}")
    print(f"\n  Wrote: {out}/canonical_schema.json")
    print(f"         {out}/field_alias_map.csv   <-- REVIEW THIS")
    print(f"         {out}/pa_forms.db")
    db.close()


if __name__ == "__main__":
    main()
