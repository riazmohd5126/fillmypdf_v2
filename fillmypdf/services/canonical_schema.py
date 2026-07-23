"""
canonical_schema.py
===================
Derive a **form intake schema** from a locked/canonical field map.

A canonical map is ``{field_name: {"canonical": <path>, ...}}`` — i.e. *which PDF
widget maps to which canonical path*. For data intake we want the inverse view:
*which canonical fields does this form need?* — so we can auto-generate a clean
web form and a matching CSV template instead of asking users to hand-write JSON.

``derive_schema`` inverts the map, drops ``other``/unmapped/unknown paths, and
enriches each canonical path with metadata from ``pa_canonical.CATALOG``
(``type``, ``required``, ``critical``, ``sensitive``, a human label) plus the
list of PDF fields it drives. Fields are returned both flat (CATALOG order) and
grouped by entity (patient / insurance / prescriber / …) for the UI.

PHI-free: this describes the blank form only; no patient values are involved.
"""

from __future__ import annotations

from typing import Dict, List

from ..models.pa_canonical import CATALOG, BY_PATH, CRITICAL_FIELDS


# Human titles for each canonical entity (path prefix).
_ENTITY_TITLES = {
    "patient": "Patient",
    "insurance": "Insurance",
    "prescriber": "Prescriber",
    "facility": "Facility",
    "medication": "Medication",
    "clinical": "Clinical",
    "request": "Request",
}

# Leaf-name overrides where a plain title-case looks wrong.
_LABEL_OVERRIDES = {
    "dob": "Date of Birth",
    "npi": "NPI",
    "ndc": "NDC",
    "ssn": "SSN",
    "dea": "DEA",
    "zip": "ZIP",
    "ptan": "PTAN",
    "tax_id": "Tax ID",
    "rx_bin": "Rx BIN",
    "rx_pcn": "Rx PCN",
    "rx_group": "Rx Group",
    "sig": "Sig / Directions",
    "hcpcs_jcode": "HCPCS / J-Code",
    "cpt_code": "CPT Code",
    "daw": "Dispense as Written",
    "primary_diagnosis_code": "Primary Diagnosis (ICD-10)",
    "primary_diagnosis_description": "Diagnosis Description",
    "member_id": "Member ID",
    "address_line1": "Address",
    "address_line2": "Address Line 2",
    "is_expedited": "Expedited / Urgent",
    "previous_auth_number": "Previous Auth #",
}


def friendly_label(path: str) -> str:
    """Human-readable label for a canonical path (e.g. ``patient.dob`` → 'Date of Birth')."""
    leaf = path.split(".")[-1]
    if leaf in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[leaf]
    return leaf.replace("_", " ").title()


def entity_title(entity: str) -> str:
    return _ENTITY_TITLES.get(entity, entity.replace("_", " ").title())


def _invert(mappings: Dict[str, dict]) -> Dict[str, List[str]]:
    """canonical path → [field names], keeping only real catalog paths."""
    by_path: Dict[str, List[str]] = {}
    for field_name, m in (mappings or {}).items():
        if not isinstance(m, dict):
            continue
        path = m.get("canonical")
        if not path or path == "other" or path not in BY_PATH:
            continue
        by_path.setdefault(path, []).append(str(field_name))
    return by_path


def derive_schema(mappings: Dict[str, dict]) -> dict:
    """Build the intake schema for a form from its canonical map.

    Returns::

        {
          "fields": [ {canonical, type, required, critical, sensitive,
                       label, form_fields:[...]}, ... ],   # CATALOG order
          "groups": [ {entity, title, fields:[ ...same objects... ]}, ... ],
        }
    """
    by_path = _invert(mappings)

    fields: List[dict] = []
    groups: Dict[str, dict] = {}

    # Iterate CATALOG so output order is stable and human-sensible.
    for cf in CATALOG:
        if cf.path not in by_path:
            continue
        entry = {
            "canonical": cf.path,
            "type": cf.type,
            "required": bool(cf.required),
            "critical": cf.path in CRITICAL_FIELDS,
            "sensitive": bool(cf.sensitive),
            "repeating": bool(cf.repeating),
            "label": friendly_label(cf.path),
            "form_fields": sorted(by_path[cf.path]),
        }
        fields.append(entry)

        entity = cf.path.split(".")[0]
        grp = groups.setdefault(entity, {
            "entity": entity,
            "title": entity_title(entity),
            "fields": [],
        })
        grp["fields"].append(entry)

    # Preserve entity order by first appearance in CATALOG.
    ordered_entities: List[str] = []
    for cf in CATALOG:
        ent = cf.path.split(".")[0]
        if ent in groups and ent not in ordered_entities:
            ordered_entities.append(ent)

    return {
        "fields": fields,
        "groups": [groups[e] for e in ordered_entities],
    }


def schema_csv_headers(schema: dict) -> List[str]:
    """Canonical-path header row for a batch CSV template (round-trips on upload)."""
    return [f["canonical"] for f in schema.get("fields", [])]
