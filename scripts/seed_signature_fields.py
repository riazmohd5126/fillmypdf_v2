"""
Seed signature_fields into existing template manifests.
Run once: python3 scripts/seed_signature_fields.py
"""
import json
from pathlib import Path

SIG_FIELDS = {
    "acord_125_commercial": [
        {"key": "applicant_signature", "label": "Applicant Signature",
         "page_index": 0, "x_pct": 5, "y_pct": 4, "width_pct": 45, "height_pct": 8,
         "required": True, "description": "Named insured / applicant signature"},
        {"key": "producer_signature", "label": "Producer / Agent Signature",
         "page_index": 0, "x_pct": 55, "y_pct": 4, "width_pct": 40, "height_pct": 8,
         "required": True, "description": "Producing agent signature"},
    ],
    "acord_126_cgl": [
        {"key": "authorized_signature", "label": "Authorized Representative Signature",
         "page_index": 0, "x_pct": 5, "y_pct": 4, "width_pct": 45, "height_pct": 8,
         "required": True, "description": "Authorized rep signature"},
    ],
    "acord_127_business_auto": [
        {"key": "applicant_signature", "label": "Applicant Signature",
         "page_index": 0, "x_pct": 5, "y_pct": 4, "width_pct": 45, "height_pct": 8,
         "required": True},
        {"key": "producer_signature", "label": "Producer Signature",
         "page_index": 0, "x_pct": 55, "y_pct": 4, "width_pct": 40, "height_pct": 8,
         "required": False},
    ],
    "acord_130_workers_comp": [
        {"key": "employer_signature", "label": "Employer / Applicant Signature",
         "page_index": 0, "x_pct": 5, "y_pct": 4, "width_pct": 45, "height_pct": 8,
         "required": True},
    ],
    "acord_140_property": [
        {"key": "applicant_signature", "label": "Applicant Signature",
         "page_index": 0, "x_pct": 5, "y_pct": 4, "width_pct": 45, "height_pct": 8,
         "required": True},
    ],
    "pa_botox_neighborhood_ri": [
        {"key": "prescriber_signature", "label": "Prescriber Signature",
         "page_index": 4, "x_pct": 5, "y_pct": 5, "width_pct": 45, "height_pct": 10,
         "required": True, "description": "Licensed prescriber — last page"},
    ],
    "pa_xifaxan_caremark": [
        {"key": "prescriber_signature", "label": "Prescriber Signature",
         "page_index": 0, "x_pct": 5, "y_pct": 4, "width_pct": 45, "height_pct": 10,
         "required": True},
    ],
    "pa_xifaxan_gateway": [
        {"key": "prescriber_signature", "label": "Prescriber Signature",
         "page_index": 0, "x_pct": 5, "y_pct": 4, "width_pct": 45, "height_pct": 10,
         "required": True},
    ],
    "pa_linzess_molina_tx": [
        {"key": "prescriber_signature", "label": "Prescriber Signature",
         "page_index": 0, "x_pct": 5, "y_pct": 4, "width_pct": 45, "height_pct": 10,
         "required": True},
    ],
    "pa_viberzi_selecthealth": [
        {"key": "prescriber_signature", "label": "Prescriber Signature",
         "page_index": 0, "x_pct": 5, "y_pct": 4, "width_pct": 45, "height_pct": 10,
         "required": True},
    ],
    "pa_alosetron_rxadvance": [
        {"key": "prescriber_signature", "label": "Prescriber Signature",
         "page_index": 0, "x_pct": 5, "y_pct": 4, "width_pct": 45, "height_pct": 10,
         "required": True},
    ],
}

base = Path(__file__).parent.parent / "fillmypdf" / "storage" / "templates"
updated, skipped = [], []

for tid, fields in SIG_FIELDS.items():
    mpath = base / tid / "manifest.json"
    if not mpath.exists():
        skipped.append(tid)
        continue
    data = json.loads(mpath.read_text())
    data["signature_fields"] = fields
    mpath.write_text(json.dumps(data, indent=2))
    updated.append(tid)

print(f"Updated {len(updated)} manifests: {updated}")
if skipped:
    print(f"Skipped {len(skipped)} (not found): {skipped}")
