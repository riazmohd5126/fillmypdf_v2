"""
field_classifier.py
===================
Split a form's widgets into the four buckets the intake pipeline treats
differently.

Only ONE of these is worth canonicalizing. Measured over 40 sampled PA forms
(4,789 widgets), plain text fields resolve to a catalog path 81% of the time,
but checkbox/radio widgets resolve at 39% — and 94% of *those* point at a
text-typed path (``prescriber.npi``, ``medication.drug_name``) that a tick box
physically cannot produce. Checkbox questions also vary form to form, so a
fixed catalog can never cover them.

So:

``data``       plain text/choice inputs → canonical catalog (patient.dob, …).
``choice``     checkbox / radio        → kept verbatim per form (FormSpec).
``longtext``   multiline textarea      → kept verbatim per form (FormSpec).
``signature``  /Sig widgets, or /Tx    → kept separate, assigned a signer role.
               blanks whose name/label is clearly a signature line
               (CareFirst-style ``Prescriber Signature`` text boxes).

The three non-``data`` buckets live in the per-form spec rather than the
canonical map, which is what stops the bogus mappings above from being created
at all.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

DATA = "data"
CHOICE = "choice"
LONGTEXT = "longtext"
SIGNATURE = "signature"

#: Buckets stored verbatim in the per-form spec instead of the canonical map.
FORM_SPECIFIC_KINDS = (CHOICE, LONGTEXT, SIGNATURE)

# A wordy caption is a weak hint on its own ("Please provide the patient's full
# legal name as printed on the card" is still patient.full_name), so length
# alone never routes a field out of the canonical bucket — it only does so when
# the catalog also failed to resolve it. See ``is_long_question``.
LONG_LABEL_WORDS = 12

# Printed signature-line captions authored as ordinary text widgets (/Tx), not
# true PDF signature fields (/Sig). Keep these out of the canonical map.
_SIGNATURE_TEXT_RE = re.compile(
    r"(?:"
    r"\b(?:prescriber|physician|provider|doctor|applicant|patient|member|"
    r"representative|pharmacist|witness|authorized|subscriber)?\s*['’]?\s*"
    r"s?\s*signature\b"
    r"|\bsignature\s+of\b"
    r"|\bsigned\s+by\b"
    r"|\bsign\s+here\b"
    r")",
    re.I,
)
_NOT_SIGNATURE_TEXT_RE = re.compile(
    r"\b(signature\s+on\s+file|electronic\s+signature\s+consent|"
    r"consent\s+to\s+sign|authorize.{0,20}signature)\b",
    re.I,
)


def is_signature_text_field(name: str, label: str = "") -> bool:
    """True for /Tx blanks that are clearly a printed signature line."""
    n = (name or "").strip()
    lab = (label or "").strip()
    for blob in (n, lab):
        if not blob:
            continue
        if _NOT_SIGNATURE_TEXT_RE.search(blob):
            continue
        if _SIGNATURE_TEXT_RE.search(blob) and len(blob.split()) <= 8:
            return True
    return False


def field_kind(field: dict, label: str = "") -> str:
    """Bucket a widget from ``_get_fields_with_coords`` by its AcroForm type."""
    ftype = str(field.get("type") or "")
    if "/Sig" in ftype:
        return SIGNATURE
    if "/Btn" in ftype:
        return CHOICE
    if "/Tx" in ftype and field.get("multiline"):
        return LONGTEXT
    if "/Tx" in ftype and is_signature_text_field(
        str(field.get("name") or ""),
        label or str(field.get("label") or ""),
    ):
        return SIGNATURE
    return DATA


def is_canonical_candidate(field: dict) -> bool:
    """True when this widget should be offered to the canonical mapper."""
    return field_kind(field) == DATA


def is_long_question(label: Optional[str]) -> bool:
    """True for captions long enough to read as a question rather than a caption.

    Used as a *secondary* signal: a field is only reclassified as ``longtext``
    when the catalog already failed to resolve it, so a verbose caption on an
    otherwise ordinary field is never lost.
    """
    return len((label or "").split()) >= LONG_LABEL_WORDS


def classify_fields(fields_info: Iterable[dict]) -> Dict[str, List[dict]]:
    """Group widgets into ``{kind: [field, ...]}``, preserving PDF order."""
    out: Dict[str, List[dict]] = {DATA: [], CHOICE: [], LONGTEXT: [], SIGNATURE: []}
    for f in fields_info:
        if not f.get("name"):
            continue
        out[field_kind(f)].append(f)
    return out


def form_specific_map_keys(fields_info: Iterable[dict]) -> set:
    """Keys that belong in the FormSpec, never the canonical map.

    Includes both ``name`` and ``name::export`` so older polluted caches that
    stored bare checkbox names or per-option keys are fully removable.
    """
    from ..models.pa_canonical import map_field_key

    keys: set = set()
    for f in fields_info:
        if not f.get("name") or field_kind(f) == DATA:
            continue
        name = str(f["name"])
        keys.add(name)
        keys.add(map_field_key(f))
        exp = f.get("export_value")
        if exp:
            keys.add(f"{name}::{exp}")
    return keys


def prune_form_specific_mappings(
    mappings: Dict[str, dict],
    fields_info: Iterable[dict],
) -> tuple:
    """Drop checkbox/longtext/signature rows from a canonical map.

    Branching questions (A/B/C Yes–No) live only in the per-form FormSpec.
    Returns ``(cleaned_mappings, dropped_count)``.
    """
    drop = form_specific_map_keys(fields_info)
    if not drop:
        # Still strip obvious choice keys when fields_info is unavailable.
        drop = {k for k in mappings if "::" in k}
    cleaned = {k: v for k, v in mappings.items() if k not in drop}
    return cleaned, len(mappings) - len(cleaned)


def field_kinds_for(fields_info: Iterable[dict]) -> Dict[str, str]:
    """``{map_key: kind}`` for every widget (also indexes bare AcroForm names)."""
    from ..models.pa_canonical import map_field_key

    out: Dict[str, str] = {}
    for f in fields_info:
        if not f.get("name"):
            continue
        kind = field_kind(f)
        out[map_field_key(f)] = kind
        out[str(f["name"])] = kind
    return out


def is_section_title_field(name: str, label: str = "") -> bool:
    """True for AcroForm text widgets that are really section headers.

    Some PDFs author a fillable box whose ``/T`` is the printed section title
    (e.g. ``Medication History for this Diagnosis``). Those must not appear as
    canonical intake fields.
    """
    n = (name or "").strip().lower()
    if not n:
        return False
    # Exact / near-exact known section headers used as field names.
    if n in {
        "medication history for this diagnosis",
        "drug information",
        "provider information",
        "member information",
        "rationale for request / pertinent clinical information",
    }:
        return True
    # Name equals a long Title-Case phrase with no trailing colon (not a caption).
    lab = (label or "").strip().lower().rstrip(":")
    if lab and lab == n and len(n.split()) >= 4:
        return True
    return False
