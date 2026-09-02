#!/usr/bin/env python3
"""
pa_rheum_derm_profiler.py — pa_profiler.py's coverage report, but broken out
by the axes that matter for a rheum/derm test corpus: SPECIALTY, DRUG CLASS
and PAYER, on top of the existing structural/mechanic axes.

Reuses pa_profiler.py's per-PDF profiling (profile_form) unmodified and
extends its SEMANTIC dict in place with rheum/derm clinical concepts (PASI/
BSA, DAS28/CDAI, TB/hepatitis screening, conventional-DMARD step therapy —
see pa_rheum_derm_taxonomy.EXTRA_SEMANTIC) so those show up as gaps too, not
just member_id/npi/dob.

Expects the folder layout pa_rheum_derm_harvester.py produces:
    <root>/<structural_type>/<specialty>/<drug_class>/<payer>/<file>.pdf
but falls back to text/filename sniffing against the drug taxonomy for
forms harvested another way (e.g. pa_form_harvester.py's flat <payer>/
layout), so this also works as a "how much of my EXISTING corpus is
actually rheum/derm, and how diverse is it" report.

USAGE:
  pip install "pypdf[crypto]"
  python3 pa_rheum_derm_profiler.py --root pa_forms_rheum_derm --out report_rheum_derm
  python3 pa_rheum_derm_profiler.py --root pa_forms --out report_rheum_derm   # mine an existing corpus
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pa_profiler as base  # noqa: E402  (reuse profile_form as-is)
from pa_rheum_derm_taxonomy import ALL_DRUGS, DRUG_CLASSES, EXTRA_SEMANTIC  # noqa: E402

# Extend the SHARED module-level dict pa_profiler.profile_form closes over,
# so profile_form's existing semantic-tagging loop picks these up for free.
base.SEMANTIC.update(EXTRA_SEMANTIC)


def path_meta(p: Path, root: Path) -> dict:
    """Recover (specialty, drug_class, payer) from the harvester's folder
    layout when present; otherwise sniff drug/specialty from the path text."""
    try:
        parts = p.relative_to(root).parts
    except ValueError:
        parts = p.parts
    meta = {"specialty": "", "drug_class": "", "drug": "", "payer": p.parent.name}
    # harvester layout: <structural_type>/<specialty>/<drug_class>/<payer>/<file>
    if len(parts) >= 4 and parts[0] in ("acroform", "xfa", "form-flat", "document", "unreadable"):
        meta["specialty"] = parts[1]
        meta["drug_class"] = parts[2].replace("-", " ")
        meta["payer"] = parts[3]
        return meta
    # fallback: sniff from the full path text against the drug taxonomy
    hay = "/".join(parts).lower()
    best = None
    for drug, dmeta in ALL_DRUGS.items():
        if drug.lower() in hay and (best is None or len(drug) > len(best[0])):
            best = (drug, dmeta)
    if best:
        meta["drug"] = best[0]
        meta["drug_class"] = best[1]["class"]
        meta["specialty"] = best[1]["specialty"]
    return meta


def profile_specialty(path: Path, root: Path) -> dict:
    row = base.profile_form(path)
    row.update(path_meta(path, root))
    # a form the taxonomy can't tie to a specific drug can still be
    # specialty-relevant by the condition terms it mentions in text
    if not row["drug"]:
        for drug, dmeta in ALL_DRUGS.items():
            if drug.lower() in row["semantic_tags"] or drug.lower() in str(path).lower():
                row["drug"] = drug
                row["drug_class"] = dmeta["class"]
                row["specialty"] = dmeta["specialty"]
                break
    return row


def report(rows: list[dict]) -> None:
    n = len(rows)
    print(f"\n{'='*62}\nRHEUM/DERM PROFILE — {n} forms\n{'='*62}")

    tagged = [r for r in rows if r["specialty"]]
    print(f"\nForms tied to a rheum/derm drug: {len(tagged)}/{n}")

    by_spec = Counter(r["specialty"] or "untagged" for r in rows)
    print("\nBy specialty:")
    for k, v in by_spec.most_common():
        print(f"  {k:10} {v:4}")

    print("\nBy drug class (variety target — every class should be non-zero):")
    by_class = defaultdict(lambda: {"n": 0, "payers": set(), "structs": set()})
    for r in tagged:
        c = by_class[r["drug_class"] or "unclassified"]
        c["n"] += 1
        if r["payer"]:
            c["payers"].add(r["payer"])
        c["structs"].add(r["structural_type"])
    for cls in DRUG_CLASSES:
        c = by_class.get(cls, {"n": 0, "payers": set(), "structs": set()})
        flag = "  <-- GAP" if c["n"] == 0 else ("  <-- thin" if c["n"] < 3 else "")
        print(f"  {cls:22} {c['n']:4}  payers:{len(c['payers']):2}  "
              f"structural:{len(c['structs']):2}{flag}")

    print("\nBy payer (top 15 by form count):")
    by_payer = Counter(r["payer"] for r in tagged if r["payer"])
    for k, v in by_payer.most_common(15):
        print(f"  {k:22} {v:4}")

    print("\nStructural type mix (tagged forms only):")
    st = Counter(r["structural_type"] for r in tagged)
    for k, v in st.most_common():
        print(f"  {k:16} {v:4}")

    # rheum/derm-specific semantic coverage — the clinical questions that
    # actually decide these PAs, not just demographic fields
    sem = Counter()
    for r in tagged:
        for t in r["semantic_tags"].split("|"):
            if t:
                sem[t] += 1
    print("\nRheum/derm clinical-question coverage:")
    for k in EXTRA_SEMANTIC:
        c = sem.get(k, 0)
        flag = "  <-- GAP" if c < 3 else ""
        print(f"  {k:24} {c:4}{flag}")

    n_drugs = len({r["drug"] for r in tagged if r["drug"]})
    n_payers = len({r["payer"] for r in tagged if r["payer"]})
    n_classes = len({r["drug_class"] for r in tagged if r["drug_class"]})
    print(f"\nDistinct drugs: {n_drugs}/{len(ALL_DRUGS)}   "
          f"drug classes: {n_classes}/{len(DRUG_CLASSES)}   payers: {n_payers}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="pa_forms_rheum_derm")
    ap.add_argument("--out", default="report_rheum_derm")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"!! {root} not found. Run pa_rheum_derm_harvester.py first "
              f"(or point --root at an existing corpus to mine it).")
        sys.exit(1)
    pdfs = sorted(root.rglob("*.pdf"))
    if not pdfs:
        print(f"!! no PDFs under {root}")
        sys.exit(1)

    print(f"Profiling {len(pdfs)} PDFs under {root} ...")
    rows = []
    for i, p in enumerate(pdfs, 1):
        rows.append(profile_specialty(p, root))
        if i % 25 == 0:
            print(f"  ...{i}/{len(pdfs)}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cols = list(rows[0].keys())
    with (out / "profile_rheum_derm.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    report(rows)
    print(f"\nWrote {out/'profile_rheum_derm.csv'}")


if __name__ == "__main__":
    main()
