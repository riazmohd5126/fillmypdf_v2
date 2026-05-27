#!/usr/bin/env python3
"""
Seed the template library toward a target catalog size (default 50).

Existing templates are left unchanged. For each missing slot, creates a manifest
stub and copies ``template.pdf`` from a donor template (default: pa_linzess_molina_tx).

Replace stub PDFs with real payer forms over time via POST /api/v1/templates (admin).

Usage:
  python3 scripts/seed_template_catalog.py
  python3 scripts/seed_template_catalog.py --target 50 --donor pa_linzess_molina_tx
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fillmypdf.config import settings  # noqa: E402

DRUGS = [
    ("Linzess", "linaclotide"),
    ("Xifaxan", "rifaximin"),
    ("Viberzi", "eluxadoline"),
    ("Amitiza", "lubiprostone"),
    ("Trulance", "plecanatide"),
    ("Motegrity", "prucalopride"),
    ("Zelnorm", "tegaserod"),
    ("Linzess", "linaclotide"),
]
PAYERS = [
    ("Molina Healthcare", "medicaid"),
    ("Caremark", "commercial"),
    ("Express Scripts", "commercial"),
    ("OptumRx", "commercial"),
    ("Medicaid FFS", "medicaid"),
    ("Medicare Part D", "medicare"),
    ("Humana", "commercial"),
    ("Aetna", "commercial"),
]
STATES = ["TX", "RI", "FL", "CA", "NY", "OH", "GA", "PA", "IL", "NC"]


def _existing_ids(templates_dir: Path) -> set[str]:
    ids: set[str] = set()
    if not templates_dir.is_dir():
        return ids
    for entry in templates_dir.iterdir():
        if entry.is_dir() and (entry / "manifest.json").exists():
            ids.add(entry.name)
    return ids


def _stub_manifest(template_id: str, idx: int) -> dict:
    drug_name, generic = DRUGS[idx % len(DRUGS)]
    payer_name, plan_type = PAYERS[idx % len(PAYERS)]
    state = STATES[idx % len(STATES)]
    return {
        "id": template_id,
        "name": f"{drug_name} PA — {payer_name} ({state}) [catalog stub]",
        "category": "prior_authorization",
        "specialty": "gi_motility",
        "drug": {"name": drug_name, "generic_name": generic, "strengths": [], "form": "capsule"},
        "payer": {"name": payer_name, "plan_type": plan_type, "state": state},
        "indications": ["catalog placeholder — replace PDF and manifest with real form"],
        "questions": [],
        "tags": ["catalog_stub", plan_type, state.lower()],
        "is_public": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed template catalog stubs")
    parser.add_argument("--target", type=int, default=50, help="Total templates on disk")
    parser.add_argument("--donor", default="pa_linzess_molina_tx", help="Donor template id for PDF copy")
    args = parser.parse_args()

    templates_dir = settings.STORAGE_DIR / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    existing = _existing_ids(templates_dir)
    donor_dir = templates_dir / args.donor
    donor_pdf = donor_dir / "template.pdf"
    if not donor_pdf.is_file():
        print(f"Donor PDF missing: {donor_pdf}", file=sys.stderr)
        return 1

    need = max(0, args.target - len(existing))
    if need == 0:
        print(f"Already have {len(existing)} templates (target {args.target}). Nothing to add.")
        return 0

    created = 0
    idx = 0
    while created < need:
        slug_drug = DRUGS[idx % len(DRUGS)][1].replace(" ", "_")
        slug_payer = PAYERS[idx % len(PAYERS)][0].lower().replace(" ", "_")[:12]
        state = STATES[idx % len(STATES)].lower()
        template_id = f"pa_{slug_drug}_{slug_payer}_{state}_stub{idx}"
        idx += 1
        if template_id in existing:
            continue
        dest = templates_dir / template_id
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(donor_pdf, dest / "template.pdf")
        (dest / "manifest.json").write_text(
            json.dumps(_stub_manifest(template_id, idx), indent=2),
            encoding="utf-8",
        )
        existing.add(template_id)
        created += 1
        print(f"  + {template_id}")

    print(f"Created {created} stub(s). Catalog size now {len(existing)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
