#!/usr/bin/env python3
"""
pa_rheum_derm_extractor.py — pull the rheumatology + dermatology subset out
of a corpus you ALREADY downloaded, with ZERO network calls.

Why this is a different tool from pa_rheum_derm_harvester.py: that one goes
out and fetches new PDFs from the web (search-API dorks, payer-portal
crawling). If you already ran pa_form_harvester.py — or collected forms some
other way — and just want the rheum/derm slice of what's sitting on disk,
you don't want to re-download anything or spend search-API quota. This is a
pure filesystem walk + classify + copy/symlink. No requests, no playwright.

MATCH LOGIC (per PDF), cheapest check first:
  1. filename/path sniff — a drug brand name appears in the file path
     (fast, no PDF parse; catches "...__Humira_PA_Request_Form.pdf")
  2. text sniff — first few pages' text mentions a drug brand name (for
     forms with an opaque filename like "Form1234.pdf")
  3. condition-only fallback — no drug brand matched, but a rheum/derm
     CONDITION term appears in the text (e.g. "psoriatic arthritis") —
     lower confidence, tagged without a drug_class

Matched forms are copied (or --symlink'd) into the SAME
<structural_type>/<specialty>/<drug_class>/<payer>/<file> layout
pa_rheum_derm_harvester.py produces, so pa_schema_extractor.py /
pa_vision_mapper.py / pa_stratify.py all work unmodified afterward, and
pa_rheum_derm_profiler.py's coverage report reads it the same way.

IDEMPOTENT: hashes existing output once at startup and skips anything
already extracted, so re-running after adding more source files to --root
only copies what's new — it will never re-copy or duplicate a form you
already pulled out.

USAGE:
  pip install "pypdf[crypto]"
  # Everything you already downloaded under pa_forms/ -> rheum+derm slice:
  python3 pa_rheum_derm_extractor.py --root pa_forms --out pa_forms_rheum_derm

  # Just see what would be pulled, no files touched:
  python3 pa_rheum_derm_extractor.py --root pa_forms --dry-run

  # Only one specialty, and don't duplicate bytes:
  python3 pa_rheum_derm_extractor.py --root pa_forms --specialty derm --symlink
"""

import argparse
import csv
import hashlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pa_rheum_derm_taxonomy import (  # noqa: E402
    ALL_DRUGS, DRUG_CLASSES, RHEUM_CONDITIONS, DERM_CONDITIONS,
)

MANIFEST_COLS = ["dest", "source", "structural_type", "specialty", "drug",
                  "drug_class", "condition", "payer", "match_basis"]

# ------------------------------------------------------------- CLASSIFY
# Same acroform/xfa/form-flat/document/unreadable split pa_form_harvester.py
# uses, so a "document" (prose clinical-criteria PDF, not a fillable form)
# in your existing corpus doesn't get miscounted as a PA form.
FORM_TOKENS = ["patient name", "date of birth", "dob", "member id", "fax",
               "prescriber", "npi", "signature", "diagnosis", "icd",
               "authorization request", "request form"]


def classify_pdf(reader) -> str:
    try:
        root = reader.trailer["/Root"]
        if "/AcroForm" in root:
            if "/XFA" in root["/AcroForm"]:
                return "xfa"
            fields = reader.get_fields()
            if fields:
                return f"acroform({len(fields)})"
    except Exception:
        pass
    text = ""
    for pg in reader.pages[:3]:
        try:
            text += (pg.extract_text() or "").lower()
        except Exception:
            pass
    score = sum(t in text for t in FORM_TOKENS)
    score += text.count("___") // 3
    score += text.count("☐")
    return "form-flat" if score >= 3 else "document"


def category(kind: str) -> str:
    if kind.startswith("acroform"):
        return "acroform"
    if kind == "xfa":
        return "xfa"
    if kind == "form-flat":
        return "form-flat"
    if kind == "document":
        return "document"
    return "unreadable"


def slugify(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower() or "unknown"


def open_reader(path: Path):
    from pypdf import PdfReader
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            pass
    return reader


def text_head(reader, pages: int = 3) -> str:
    out = ""
    for pg in reader.pages[:pages]:
        try:
            out += (pg.extract_text() or "") + " "
        except Exception:
            pass
    return out


def match_drug(hay: str) -> tuple[str, dict] | tuple[None, None]:
    """Longest brand-name match wins (so e.g. a hypothetical short brand
    name can't shadow a more specific one that also appears).

    Uses an alphanumeric-boundary check rather than regex \\b: real-world
    downloaded filenames are commonly underscore-delimited
    ("Humira_PA_Request_Form.pdf"), and \\b treats "_" as a word character
    — so "_humira_" has NO word boundary and \\bhumira\\b silently fails to
    match. Requiring only non-alphanumeric neighbors catches "_", "-", ".",
    spaces, and string edges alike.
    """
    hay_l = hay.lower()
    best = None
    for drug, meta in ALL_DRUGS.items():
        pattern = r"(?<![a-z0-9])" + re.escape(drug.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, hay_l):
            if best is None or len(drug) > len(best[0]):
                best = (drug, meta)
    return best if best else (None, None)


def match_condition(hay: str) -> tuple[str, str] | tuple[None, None]:
    """Fallback when no drug brand matched: tag by condition term alone."""
    hay_l = hay.lower()
    for cond in RHEUM_CONDITIONS:
        if cond in hay_l:
            return ("rheum", cond)
    for cond in DERM_CONDITIONS:
        if cond in hay_l:
            return ("derm", cond)
    return (None, None)


# --------------------------------------------------------------- HASHING
def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def existing_output_hashes(out: Path) -> set[str]:
    hashes = set()
    if not out.exists():
        return hashes
    for p in out.rglob("*.pdf"):
        try:
            hashes.add(hash_file(p))
        except Exception:
            pass
    return hashes


# ------------------------------------------------------------------ MAIN
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="pa_forms",
                    help="folder of PDFs you already downloaded")
    ap.add_argument("--out", default="pa_forms_rheum_derm")
    ap.add_argument("--specialty", choices=["rheum", "derm", "both"], default="both",
                    help="keep only this specialty (a drug tagged 'both' always passes)")
    ap.add_argument("--include-documents", action="store_true",
                    help="also pull prose/clinical-criteria PDFs, not just fillable forms")
    ap.add_argument("--symlink", action="store_true", help="symlink instead of copy — no duplicated bytes")
    ap.add_argument("--dry-run", action="store_true", help="report matches, touch no files")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"!! {root} not found. Point --root at the corpus you already downloaded.")
        sys.exit(1)
    pdfs = sorted(root.rglob("*.pdf"))
    if not pdfs:
        print(f"!! no PDFs under {root}")
        sys.exit(1)

    out = Path(args.out)
    seen_hashes = existing_output_hashes(out)
    already = len(seen_hashes)
    print(f"Scanning {len(pdfs)} PDFs under {root} "
          f"({already} already extracted to {out}, will be skipped) ...")

    manifest_rows = []
    n_matched = n_dupe = n_no_match = n_skipped_doc = n_unreadable = 0
    struct_counts, spec_counts = Counter(), Counter()
    class_counts = defaultdict(lambda: {"n": 0, "payers": set()})

    for i, path in enumerate(pdfs, 1):
        try:
            reader = open_reader(path)
        except Exception:
            n_unreadable += 1
            continue

        kind = classify_pdf(reader)
        cat = category(kind)
        if not args.include_documents and cat in ("document", "unreadable"):
            n_skipped_doc += 1
            continue

        basis = "filename"
        drug, meta = match_drug(str(path))
        if drug is None:
            text = text_head(reader)
            basis = "text"
            drug, meta = match_drug(text)
        else:
            text = None

        if drug:
            specialty, drug_class, condition = meta["specialty"], meta["class"], meta["conditions"][0]
        else:
            if text is None:
                text = text_head(reader)
            specialty, condition = match_condition(text if text else str(path))
            drug_class = ""
            basis = "condition-text" if specialty else basis
            if specialty is None:
                n_no_match += 1
                continue

        if args.specialty != "both" and specialty not in (args.specialty, "both"):
            n_no_match += 1
            continue

        try:
            content_hash = hash_file(path)
        except Exception:
            n_unreadable += 1
            continue
        if content_hash in seen_hashes:
            n_dupe += 1
            continue

        payer = path.parent.name  # both harvesters' layouts put payer/host as the immediate folder
        dest_dir = out / cat / specialty / slugify(drug_class or condition) / slugify(payer)
        dest = dest_dir / f"{slugify(payer)}_{slugify(path.stem)[:50]}_{content_hash}.pdf"

        struct_counts[cat] += 1
        spec_counts[specialty] += 1
        c = class_counts[drug_class or f"condition:{condition}"]
        c["n"] += 1
        c["payers"].add(payer)
        manifest_rows.append([str(dest.relative_to(out)) if not args.dry_run else str(dest),
                              str(path), cat, specialty, drug or "", drug_class, condition, payer, basis])
        n_matched += 1
        seen_hashes.add(content_hash)

        if not args.dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            if args.symlink:
                dest.symlink_to(path.resolve())
            else:
                import shutil
                shutil.copy2(path, dest)

        tag = f"{specialty}/{drug_class or ('cond:' + condition)}"
        action = "[would extract]" if args.dry_run else "[extracted]"
        print(f"  {action} ({tag}, via {basis}) {path.name}")

        if i % 50 == 0:
            print(f"  ...scanned {i}/{len(pdfs)}")

    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)
        manifest_path = out / "extract_manifest.csv"
        write_header = not manifest_path.exists()
        with manifest_path.open("a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(MANIFEST_COLS)
            w.writerows(manifest_rows)
    else:
        manifest_path = None

    print(f"\n{'='*60}\nRHEUM/DERM EXTRACTION{'  (dry-run)' if args.dry_run else ''}\n{'='*60}")
    print(f"  scanned:                {len(pdfs)}")
    print(f"  matched + extracted:    {n_matched}")
    print(f"  already extracted:      {n_dupe}  (skipped, same content hash already in {out})")
    print(f"  not rheum/derm:         {n_no_match}")
    print(f"  documents/unreadable:   {n_skipped_doc + n_unreadable}"
          f"{'' if args.include_documents else '  (pass --include-documents to keep these)'}")

    print("\nBy specialty:")
    for k, v in spec_counts.most_common():
        print(f"  {k:10} {v:4}")

    print("\nBy drug class / condition-only bucket:")
    for k, v in sorted(class_counts.items(), key=lambda x: -x[1]["n"]):
        print(f"  {k:26} {v['n']:4}  payers:{len(v['payers'])}")

    print("\nBy structural type:")
    for k, v in struct_counts.most_common():
        print(f"  {k:16} {v:4}")

    if manifest_path:
        print(f"\nManifest: {manifest_path}")
        print(f"Output:   {out}/<structural_type>/<specialty>/<drug_class>/<payer>/*.pdf")


if __name__ == "__main__":
    main()
