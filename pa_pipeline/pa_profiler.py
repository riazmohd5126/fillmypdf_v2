#!/usr/bin/env python3
"""
pa_profiler.py — turn a folder of harvested PA forms into a coverage report.

Walks a directory tree of PDFs, scores each form on the axes that actually
matter for an autofill engine, and tells you WHERE YOUR COVERAGE IS THIN
rather than just how many forms you have.

Outputs (next to the script, or --out DIR):
  profile.csv   one row per form, every measured axis
  clusters.csv  forms grouped by field-signature (near-duplicate templates)
  + a printed coverage report with GAP flags

USAGE:
  pip install "pypdf[crypto]"
  python3 pa_profiler.py                  # defaults to ./pa_forms
  python3 pa_profiler.py --root /path/to/pa_forms --out report

AXES MEASURED (the taxonomy):
  structural_type : acroform | xfa | flat-digital | flat-scanned
  pages, encrypted
  field counts by mechanic: text, checkbox, radio-group, combo, listbox,
                            signature, pushbutton
  comb fields, multiline fields  (the tricky-to-fill ones)
  has_text_layer  (flat forms: false => needs OCR)
  semantic_tags   (member_id, npi, ndc, icd, dob, dea, fax, prescriber...)
  field_signature : hash of sorted (name, type) -> identifies same template
"""

import argparse
import csv
import hashlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from pypdf import PdfReader

# bit masks for /Ff field flags (PDF spec, 1-indexed bits -> 0-indexed shifts)
FF_MULTILINE = 1 << 12
FF_PUSHBUTTON = 1 << 16
FF_RADIO = 1 << 15
FF_COMBO = 1 << 17
FF_COMB = 1 << 24

SEMANTIC = {
    "member_id": ["member id", "member#", "member #", "subscriber", "policy number", "memberid"],
    "patient_name": ["patient name", "member name", "patient first", "last name"],
    "npi": ["npi"],
    "ndc": ["ndc"],
    "icd": ["icd", "diagnosis code"],
    "dob": ["dob", "date of birth", "birth date", "birthdate"],
    "dea": ["dea"],
    "fax": ["fax"],
    "phone": ["phone", "telephone"],
    "prescriber": ["prescriber", "physician", "provider name", "requesting provider"],
    "drug": ["drug", "medication", "j-code", "jcode", "hcpcs"],
    "diagnosis": ["diagnosis", "dx"],
    "quantity": ["quantity", "days supply", "qty"],
}


def _flags(field) -> int:
    try:
        return int(field.get("/Ff", 0) or 0)
    except Exception:
        return 0


def profile_form(path: Path) -> dict:
    row = {"file": str(path), "payer": path.parent.name,
           "structural_type": "", "pages": 0, "encrypted": False,
           "n_text": 0, "n_checkbox": 0, "n_radio": 0, "n_combo": 0,
           "n_listbox": 0, "n_signature": 0, "n_pushbutton": 0,
           "n_comb": 0, "n_multiline": 0, "n_fields": 0,
           "has_text_layer": "", "semantic_tags": "", "field_signature": ""}
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            row["encrypted"] = True
            try:
                reader.decrypt("")
            except Exception:
                pass
        row["pages"] = len(reader.pages)

        root = reader.trailer["/Root"]
        is_xfa = "/AcroForm" in root and "/XFA" in root["/AcroForm"]
        fields = {}
        try:
            fields = reader.get_fields() or {}
        except Exception:
            fields = {}

        sig_parts, radio_names, name_blob = [], set(), []
        for name, f in fields.items():
            ft = f.get("/FT")
            ff = _flags(f)
            name_blob.append(str(name).lower())
            sig_parts.append(f"{name}:{ft}")
            if ft == "/Tx":
                row["n_text"] += 1
                if ff & FF_COMB:
                    row["n_comb"] += 1
                if ff & FF_MULTILINE:
                    row["n_multiline"] += 1
            elif ft == "/Btn":
                if ff & FF_PUSHBUTTON:
                    row["n_pushbutton"] += 1
                elif ff & FF_RADIO:
                    radio_names.add(name)       # count groups, not buttons
                else:
                    row["n_checkbox"] += 1
            elif ft == "/Ch":
                if ff & FF_COMBO:
                    row["n_combo"] += 1
                else:
                    row["n_listbox"] += 1
            elif ft == "/Sig":
                row["n_signature"] += 1
        row["n_radio"] = len(radio_names)
        row["n_fields"] = len(fields)

        # extract text (first 3 pages) for layer-detection + semantics
        text = ""
        for pg in reader.pages[:3]:
            try:
                text += (pg.extract_text() or "")
            except Exception:
                pass
        text_l = text.lower()

        # structural type
        if is_xfa:
            row["structural_type"] = "xfa"
        elif fields:
            row["structural_type"] = "acroform"
        else:
            row["has_text_layer"] = len(text.strip()) > 80
            row["structural_type"] = "flat-digital" if row["has_text_layer"] else "flat-scanned"

        # semantics: from field names if present, else from text
        hay = " ".join(name_blob) + " " + text_l
        tags = [k for k, toks in SEMANTIC.items() if any(t in hay for t in toks)]
        row["semantic_tags"] = "|".join(sorted(tags))

        # signature: acroforms by field structure; flats by normalized text
        if sig_parts:
            sig = "FORM:" + ";".join(sorted(sig_parts))
        else:
            norm = re.sub(r"\s+", " ", text_l)[:600]
            sig = "FLAT:" + norm
        row["field_signature"] = hashlib.md5(sig.encode()).hexdigest()[:12]
    except Exception as e:
        row["structural_type"] = f"error({type(e).__name__})"
    return row


def report(rows: list[dict]) -> None:
    n = len(rows)
    print(f"\n{'='*60}\nCOVERAGE REPORT — {n} forms\n{'='*60}")

    def count(pred):
        return sum(1 for r in rows if pred(r))

    # structural mix
    st = Counter(r["structural_type"] for r in rows)
    print("\nStructural type:")
    for k, v in st.most_common():
        print(f"  {k:16} {v:4}  ({v*100//n}%)")

    # field-count distribution (acroforms only)
    af = [r for r in rows if r["structural_type"] == "acroform"]
    if af:
        buckets = Counter()
        for r in af:
            fc = r["n_fields"]
            b = ("1-25" if fc <= 25 else "26-50" if fc <= 50 else
                 "51-100" if fc <= 100 else "101-150" if fc <= 150 else "150+")
            buckets[b] += 1
        print(f"\nField-count distribution ({len(af)} acroforms):")
        for b in ["1-25", "26-50", "51-100", "101-150", "150+"]:
            if buckets[b]:
                print(f"  {b:10} {buckets[b]:4}")

    # field-mechanic presence — the key coverage axis
    print("\nField-mechanic presence (forms containing >=1):")
    mech = [("checkbox", "n_checkbox"), ("radio group", "n_radio"),
            ("combo/dropdown", "n_combo"), ("listbox", "n_listbox"),
            ("signature", "n_signature"), ("comb field", "n_comb"),
            ("multiline", "n_multiline")]
    for label, key in mech:
        c = count(lambda r: r[key] > 0)
        flag = "  <-- GAP" if c < 3 else ""
        print(f"  {label:16} {c:4}{flag}")

    # OCR-needed flats
    scanned = count(lambda r: r["structural_type"] == "flat-scanned")
    print(f"\nFlat forms needing OCR (no text layer): {scanned}")
    print(f"Encrypted forms: {count(lambda r: r['encrypted'])}")
    print(f"Multi-page forms (>1 pg): {count(lambda r: r['pages'] > 1)}")

    # semantic coverage
    sem = Counter()
    for r in rows:
        for t in r["semantic_tags"].split("|"):
            if t:
                sem[t] += 1
    print("\nSemantic field coverage (forms mentioning each):")
    for k in SEMANTIC:
        c = sem.get(k, 0)
        flag = "  <-- GAP" if c < 3 else ""
        print(f"  {k:14} {c:4}{flag}")

    # clustering -> distinct templates
    clusters = defaultdict(list)
    for r in rows:
        clusters[r["field_signature"]].append(r)
    sizes = sorted((len(v) for v in clusters.values()), reverse=True)
    dups = sum(s - 1 for s in sizes)
    print(f"\n{'='*60}\nDEDUP / DISTINCTNESS\n{'='*60}")
    print(f"  Total forms:        {n}")
    print(f"  Distinct templates: {len(clusters)}  <-- your real coverage number")
    print(f"  Redundant copies:   {dups}")
    if sizes[:5]:
        print(f"  Largest clusters:   {sizes[:5]} (forms sharing one template)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="pa_forms", help="folder of harvested PDFs")
    ap.add_argument("--out", default=".", help="where to write profile/clusters CSV")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"!! {root} not found. Point --root at your harvested forms folder.")
        sys.exit(1)
    pdfs = sorted(root.rglob("*.pdf"))
    if not pdfs:
        print(f"!! no PDFs under {root}")
        sys.exit(1)

    print(f"Profiling {len(pdfs)} PDFs under {root} ...")
    rows = []
    for i, p in enumerate(pdfs, 1):
        rows.append(profile_form(p))
        if i % 25 == 0:
            print(f"  ...{i}/{len(pdfs)}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cols = list(rows[0].keys())
    with (out / "profile.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    clusters = defaultdict(list)
    for r in rows:
        clusters[r["field_signature"]].append(r)
    with (out / "clusters.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["signature", "count", "structural_type", "representative", "members"])
        for sig, members in sorted(clusters.items(), key=lambda x: -len(x[1])):
            w.writerow([sig, len(members), members[0]["structural_type"],
                        members[0]["file"], " | ".join(m["file"] for m in members)])

    report(rows)
    print(f"\nWrote {out/'profile.csv'} and {out/'clusters.csv'}")


if __name__ == "__main__":
    main()
