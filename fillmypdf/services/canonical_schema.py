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

from typing import Dict, List, Optional

from ..models.pa_canonical import CATALOG, BY_PATH, CRITICAL_FIELDS


# Human titles for each canonical entity (path prefix).
_ENTITY_TITLES = {
    "patient": "Patient",
    "insurance": "Insurance",
    "prescriber": "Prescriber",
    "requesting_provider": "Requesting Provider",
    "attending_provider": "Attending Provider",
    "billing_provider": "Billing Provider",
    "facility": "Facility",
    "pharmacy": "Pharmacy",
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
    "icd_version": "ICD Version",
    "functional_status": "Functional Status (ADLs)",
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


def _rule_for_path(mappings: Dict[str, dict], form_fields: List[str]) -> Optional[dict]:
    """Best executable rule among widgets mapped to this canonical path.

    Prefer rules on plain data widgets (skip AcroForm ``name::export`` choice
    keys) so a polluted checkbox mapping can't lock an unrelated text field.
    """
    preferred = [n for n in form_fields if "::" not in n]
    for name in preferred or form_fields:
        m = mappings.get(name)
        if isinstance(m, dict) and isinstance(m.get("rule"), dict):
            return dict(m["rule"])
    return None


def _meta_for_path(mappings: Dict[str, dict], form_fields: List[str]) -> dict:
    """linked_field / conditional / skip_logic from the richest mapped widget."""
    out: dict = {}
    for name in form_fields:
        m = mappings.get(name)
        if not isinstance(m, dict):
            continue
        for k in ("linked_field", "conditional", "skip_logic"):
            if m.get(k) and k not in out:
                out[k] = m[k]
    return out


def derive_schema(mappings: Dict[str, dict]) -> dict:
    """Build the intake schema for a form from its canonical map.

    Returns::

        {
          "fields": [ {canonical, type, required, critical, sensitive,
                       label, form_fields:[...], rule?, linked_field?, …}, ... ],
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
        form_fields = sorted(by_path[cf.path])
        entry = {
            "canonical": cf.path,
            "type": cf.type,
            "required": bool(cf.required),
            "critical": cf.path in CRITICAL_FIELDS,
            "sensitive": bool(cf.sensitive),
            "repeating": bool(cf.repeating),
            "label": friendly_label(cf.path),
            "form_fields": form_fields,
        }
        # Enum fields carry their allowed values so the UI can render a dropdown.
        if getattr(cf, "choices", ()):
            entry["options"] = [{"value": v, "label": l} for v, l in cf.choices]
        meta = _meta_for_path(mappings, form_fields)
        entry.update(meta)
        rule = _rule_for_path(mappings, form_fields)
        if rule:
            entry["rule"] = rule
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


# ---------------------------------------------------------------------------
# Full intake schema = canonical half + this form's own questions
# ---------------------------------------------------------------------------

#: Prefix marking a form-specific answer column in a CSV template, so canonical
#: paths and per-form question ids can't collide.
QUESTION_COL_PREFIX = "q:"
NARRATIVE_COL_PREFIX = "t:"


def intake_schema(mappings: Dict[str, dict], spec=None) -> dict:
    """Everything a web form needs to collect one submission for this form.

    Two halves, kept distinct because they behave differently: canonical fields
    are shared across forms and prefill from a stored profile, while questions,
    tables and narratives are this form's own wording and are asked once, here.

    ``rules`` is keyed by the thing being shown/hidden (question id, table id,
    or narrative field), so a renderer can evaluate visibility without walking
    the tree.
    """
    out = {
        "canonical": derive_schema(mappings),
        "questions": [],
        "tables": [],
        "narratives": [],
        "extras": [],
        "signatures": [],
        "rules": {},
    }
    if spec is None:
        return out

    for q in spec.questions:
        out["questions"].append({
            "id": q.id,
            "question": q.question,
            "input": q.input,
            "options": [
                {
                    "field": o.field,
                    "label": o.label,
                    "export": o.export,
                    "skip_logic": o.skip_logic,
                }
                for o in q.options
            ],
            "section": q.section,
            "subsection": q.subsection,
            "page": q.page,
            "order": q.order,
            "skip_logic": q.skip_logic,
            "conditional": q.conditional,
            "rule": q.rule.model_dump(mode="json") if q.rule else None,
            # An opt-in catalog path lets a recurring question (expedited,
            # new vs continuation) prefill from the patient profile.
            "prefill_from": q.canonical_hint,
        })
        if q.rule:
            out["rules"][q.id] = q.rule.model_dump(mode="json")

    for t in getattr(spec, "tables", None) or []:
        out["tables"].append({
            "id": t.id,
            "title": t.title,
            "section": t.section,
            "subsection": t.subsection,
            "page": t.page,
            "order": t.order,
            "row_count": t.row_count,
            "columns": [
                {"id": c.id, "header": c.header, "fields": list(c.fields)}
                for c in t.columns
            ],
            "rule": t.rule.model_dump(mode="json") if t.rule else None,
        })
        if t.rule:
            out["rules"][t.id] = t.rule.model_dump(mode="json")

    for lt in spec.long_text:
        out["narratives"].append({
            "field": lt.field,
            "label": lt.label,
            "section": lt.section,
            "subsection": lt.subsection,
            "page": lt.page,
            "order": lt.order,
            "skip_logic": lt.skip_logic,
            "conditional": lt.conditional,
            "rule": lt.rule.model_dump(mode="json") if lt.rule else None,
        })
        if lt.rule:
            out["rules"][lt.field] = lt.rule.model_dump(mode="json")

    for ex in getattr(spec, "extras", None) or []:
        out["extras"].append({
            "field": ex.field,
            "acro_field": ex.acro_field,
            "label": ex.label,
            "kind": ex.kind,
            "section": ex.section,
            "subsection": ex.subsection,
            "page": ex.page,
            "order": ex.order,
            "export": ex.export,
        })

    # Canonical dependents (e.g. How Long) — rules keyed by catalog path for the UI.
    for f in out["canonical"].get("fields", []):
        rule = f.get("rule")
        if rule:
            out["rules"][f["canonical"]] = rule

    out["signatures"] = [
        {
            "field": s.field,
            "acro_field": s.acro_field,
            "label": s.label,
            "kind": getattr(s, "kind", None) or "signature",
            "role": s.role,
            "section": s.section,
            "page": s.page,
            "order": s.order,
        }
        for s in spec.signatures
    ]
    return out


def intake_csv_headers(schema: dict) -> List[str]:
    """Header row covering both halves, so a batch CSV can fill a whole form."""
    headers = [f["canonical"] for f in schema.get("canonical", {}).get("fields", [])]
    headers += [f"{QUESTION_COL_PREFIX}{q['id']}" for q in schema.get("questions", [])]
    for t in schema.get("tables", []) or []:
        for c in t.get("columns") or []:
            for field in c.get("fields") or []:
                headers.append(f"{NARRATIVE_COL_PREFIX}{field}")
    headers += [f"{NARRATIVE_COL_PREFIX}{n['field']}" for n in schema.get("narratives", [])]
    # Leftover extras share the narrative ``t:`` prefix (direct AcroForm write).
    seen = set(headers)
    for ex in schema.get("extras", []) or []:
        field = ex.get("field") or ex.get("acro_field")
        if not field:
            continue
        col = f"{NARRATIVE_COL_PREFIX}{field}"
        if col not in seen:
            headers.append(col)
            seen.add(col)
    for s in schema.get("signatures", []) or []:
        field = s.get("field") or s.get("acro_field")
        if not field:
            continue
        col = f"{NARRATIVE_COL_PREFIX}{field}"
        if col not in seen:
            headers.append(col)
            seen.add(col)
    return headers


def intake_csv_legend(schema: dict) -> List[dict]:
    """Human-readable legend for Guided Batch CSV columns.

    Each row: ``{column, kind, label, notes}`` — used by the UI and a
    companion ``legend.json`` downloaded with the CSV template.
    """
    rows: List[dict] = []
    for f in schema.get("canonical", {}).get("fields", []) or []:
        rows.append({
            "column": f.get("canonical"),
            "kind": "canonical",
            "label": f.get("label") or f.get("canonical"),
            "type": f.get("type"),
            "required": bool(f.get("required")),
            "notes": "Shared catalog path — prefill from Patient/Provider profiles",
        })
    for q in schema.get("questions", []) or []:
        col = f"{QUESTION_COL_PREFIX}{q['id']}"
        opts = ", ".join(
            (o.get("label") or o.get("export") or "")
            for o in (q.get("options") or [])
        )
        rows.append({
            "column": col,
            "kind": "question",
            "label": q.get("question") or q.get("id"),
            "type": q.get("input") or "choice",
            "required": False,
            "notes": f"Form-specific answer. Options: {opts}" if opts else "Form-specific answer (q: prefix)",
        })
    for t in schema.get("tables", []) or []:
        title = t.get("title") or t.get("id") or "table"
        for c in t.get("columns") or []:
            header = c.get("header") or c.get("id")
            for field in c.get("fields") or []:
                rows.append({
                    "column": f"{NARRATIVE_COL_PREFIX}{field}",
                    "kind": "table_cell",
                    "label": f"{title} / {header}",
                    "type": "text",
                    "required": False,
                    "notes": "Table cell written directly to the AcroForm widget (t: prefix)",
                })
    for n in schema.get("narratives", []) or []:
        field = n.get("field")
        rows.append({
            "column": f"{NARRATIVE_COL_PREFIX}{field}",
            "kind": "narrative",
            "label": n.get("label") or field,
            "type": "text",
            "required": False,
            "notes": "Free-text / comments field (t: prefix)",
        })
    for ex in schema.get("extras", []) or []:
        field = ex.get("field") or ex.get("acro_field")
        if not field:
            continue
        rows.append({
            "column": f"{NARRATIVE_COL_PREFIX}{field}",
            "kind": "extra",
            "label": ex.get("label") or field,
            "type": ex.get("kind") or "text",
            "required": False,
            "notes": "Form-only leftover field (t: prefix)",
        })
    for s in schema.get("signatures", []) or []:
        field = s.get("field") or s.get("acro_field")
        if not field:
            continue
        kind = s.get("kind") or "signature"
        rows.append({
            "column": f"{NARRATIVE_COL_PREFIX}{field}",
            "kind": "signature" if kind != "date" else "signature_date",
            "label": s.get("label") or field,
            "type": kind,
            "required": False,
            "notes": (
                "Typed signer name for batch stamp (signature_mode=typed); "
                "dates can be MM/DD/YYYY text"
                if kind != "date"
                else "Signature date blank (t: prefix)"
            ),
        })
    return rows
