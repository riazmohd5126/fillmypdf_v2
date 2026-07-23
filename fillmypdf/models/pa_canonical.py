"""
canonical_model.py
==================
The canonical data model for the PA autofill API.

This is the "translator's notebook" that sits between input data (JSON/CSV/XLSX/EHR)
and any payer's form. Input -> canonical happens once per input format; canonical ->
form happens once per form. Two simple mappings instead of N x M.

Two things live here:
  1. FIELD_CATALOG  - every canonical field, its semantic type, its known form-label
                      aliases, and flags (required / repeating / sensitive). This is what
                      the label-matching and validation layers read.
  2. Pydantic models - the typed container the API actually fills and serializes.

The `type` strings (npi, ndc, icd10, date, member_id, ...) are the SAME ones the eval
harness keys its normalizers and validators off, so a field's type decides how it is
compared, normalized, and validated everywhere.

Sensitive=True marks PHI that must be encrypted at rest (do NOT keep these in plaintext).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from datetime import date
from typing import List, Optional

try:
    from pydantic import BaseModel, Field
except ImportError:  # schema/catalog still usable without pydantic installed
    BaseModel = object
    def Field(default=None, **_):  # type: ignore
        return default


# ---------------------------------------------------------------------------
# Semantic field types  (drive normalization + validation)
# ---------------------------------------------------------------------------
# text date number enum checkbox signature
# npi dea ndc jcode icd10 cpt member_id tax_id ssn phone email zip
# list  (repeating; see prior_therapies)


@dataclass(frozen=True)
class CanonicalField:
    path: str                       # "patient.dob"
    type: str                       # semantic type -> normalizer + validator
    aliases: tuple = ()             # lowercased form-label synonyms
    required: bool = False          # commonly mandatory on PA forms
    repeating: bool = False         # one-to-many (list)
    sensitive: bool = False         # PHI -> must be encrypted at rest
    notes: str = ""


# ---------------------------------------------------------------------------
# FIELD CATALOG  (the entire canonical model)
# ---------------------------------------------------------------------------

CATALOG: List[CanonicalField] = [

    # ---- PATIENT ----------------------------------------------------------
    CanonicalField("patient.first_name", "text",
        ("first name", "patient first name", "pt first name", "member first name"),
        required=True, sensitive=True),
    CanonicalField("patient.middle_name", "text",
        ("middle name", "middle initial", "mi"), sensitive=True),
    CanonicalField("patient.last_name", "text",
        ("last name", "patient last name", "surname", "family name"),
        required=True, sensitive=True),
    CanonicalField("patient.full_name", "text",
        ("patient name", "member name", "name", "patient"),
        sensitive=True,
        notes="Composite. Use when the form has a single name box."),
    CanonicalField("patient.dob", "date",
        ("dob", "date of birth", "birth date", "birthdate", "pt dob"),
        required=True, sensitive=True),
    CanonicalField("patient.sex", "enum",
        ("sex", "gender", "m/f"),
        notes="Normalize to M / F / X / Unknown."),
    CanonicalField("patient.address_line1", "text",
        ("address", "street", "street address", "patient address", "address line 1"),
        sensitive=True),
    CanonicalField("patient.address_line2", "text",
        ("apt", "suite", "unit", "address line 2"), sensitive=True),
    CanonicalField("patient.city", "text", ("city",), sensitive=True),
    CanonicalField("patient.state", "enum", ("state", "st"),
        notes="Two-letter US state."),
    CanonicalField("patient.zip", "zip",
        ("zip", "zip code", "postal code", "zipcode"), sensitive=True),
    CanonicalField("patient.phone", "phone",
        ("phone", "home phone", "patient phone", "telephone", "cell"),
        sensitive=True),
    CanonicalField("patient.email", "email",
        ("email", "e-mail", "patient email", "email address"), sensitive=True),
    CanonicalField("patient.ssn", "ssn",
        ("ssn", "social security", "social security number"),
        sensitive=True, notes="Rare on PA; always encrypt. Auto-detect and mask."),
    CanonicalField("patient.weight_kg", "number",
        ("weight", "wt", "weight kg", "body weight"),
        notes="Clinically relevant for weight-based biologic dosing."),
    CanonicalField("patient.height_cm", "number", ("height", "ht")),
    CanonicalField("patient.allergies", "text",
        ("allergies", "drug allergies", "known allergies")),

    # ---- INSURANCE / COVERAGE --------------------------------------------
    CanonicalField("insurance.payer_name", "text",
        ("insurance", "plan", "payer", "carrier", "insurance company", "health plan",
         "submitted to", "submit to", "plan name", "insurance name"),
        required=True),
    CanonicalField("insurance.member_id", "member_id",
        ("member id", "member #", "subscriber id", "id number", "insurance id",
         "policy number", "member number"),
        required=True, sensitive=True,
        notes="CRITICAL field: wrong value = denial. Defer if low confidence."),
    CanonicalField("insurance.group_number", "text",
        ("group", "group #", "group number", "grp")),
    CanonicalField("insurance.plan_name", "text",
        ("plan name", "plan type", "product")),
    CanonicalField("insurance.rx_bin", "text",
        ("bin", "rx bin", "rxbin"), notes="Pharmacy routing."),
    CanonicalField("insurance.rx_pcn", "text", ("pcn", "rx pcn", "rxpcn")),
    CanonicalField("insurance.rx_group", "text", ("rx group", "rxgroup")),
    CanonicalField("insurance.subscriber_name", "text",
        ("subscriber", "policyholder", "subscriber name", "insured name"),
        sensitive=True, notes="When the patient is not the subscriber."),
    CanonicalField("insurance.subscriber_relationship", "text",
        ("relationship to subscriber", "patient relationship", "relationship")),
    CanonicalField("insurance.secondary_payer_name", "text",
        ("secondary insurance", "secondary payer", "other coverage")),
    CanonicalField("insurance.secondary_member_id", "member_id",
        ("secondary member id", "secondary id"), sensitive=True),

    # ---- PRESCRIBER -------------------------------------------------------
    CanonicalField("prescriber.first_name", "text",
        ("prescriber first name", "physician first name")),
    CanonicalField("prescriber.last_name", "text",
        ("prescriber last name", "physician last name")),
    CanonicalField("prescriber.full_name", "text",
        ("prescriber", "prescriber name", "physician name", "provider name",
         "doctor", "md", "provider", "print", "printed name",
         "physician print", "prescriber print", "physician signature",
         "ordering provider", "requesting physician"),
        required=True),
    CanonicalField("prescriber.npi", "npi",
        ("npi", "prescriber npi", "provider npi", "individual npi"),
        required=True,
        notes="CRITICAL: 10-digit Luhn-checked. Reject impossible NPIs."),
    CanonicalField("prescriber.dea", "dea",
        ("dea", "dea number", "dea #"),
        notes="2 letters + 7 digits, last digit is a checksum."),
    CanonicalField("prescriber.state_license", "text",
        ("state license", "license #", "license number", "medical license")),
    CanonicalField("prescriber.specialty", "text",
        ("specialty", "provider specialty", "physician specialty")),
    CanonicalField("prescriber.phone", "phone",
        ("office phone", "provider phone", "prescriber phone")),
    CanonicalField("prescriber.fax", "phone",
        ("fax", "provider fax", "office fax", "prescriber fax", "fax number"),
        required=True, notes="PA decisions still return by fax; usually mandatory."),
    CanonicalField("prescriber.email", "email", ("provider email", "prescriber email")),
    CanonicalField("prescriber.address_line1", "text",
        ("provider address", "prescriber address", "office address")),
    CanonicalField("prescriber.city", "text", ("provider city",)),
    CanonicalField("prescriber.state", "enum", ("provider state",)),
    CanonicalField("prescriber.zip", "zip", ("provider zip",)),
    CanonicalField("prescriber.contact_name", "text",
        ("contact", "office contact", "contact person", "contact name"),
        notes="Staffer the payer calls back."),

    # ---- FACILITY / ORGANIZATION -----------------------------------------
    CanonicalField("facility.name", "text",
        ("facility", "practice name", "clinic", "organization", "office name",
         "facility name"),
        ),
    CanonicalField("facility.npi", "npi",
        ("facility npi", "group npi", "type 2 npi", "organization npi"),
        notes="Type-2 (organizational) NPI, Luhn-checked."),
    CanonicalField("facility.tax_id", "tax_id",
        ("tax id", "tin", "ein", "federal tax id", "tax id number"),
        sensitive=True, notes="9 digits."),
    CanonicalField("facility.ptan", "text",
        ("ptan", "medicare ptan"), notes="Medical PA."),
    CanonicalField("facility.address_line1", "text", ("facility address",)),
    CanonicalField("facility.city", "text", ("facility city",)),
    CanonicalField("facility.state", "enum", ("facility state",)),
    CanonicalField("facility.zip", "zip", ("facility zip",)),
    CanonicalField("facility.phone", "phone", ("facility phone",)),
    CanonicalField("facility.fax", "phone", ("facility fax",)),

    # ---- MEDICATION / REQUESTED SERVICE ----------------------------------
    CanonicalField("medication.drug_name", "text",
        ("medication", "drug", "requested medication", "product name", "drug name",
         "requested drug"),
        required=True),
    CanonicalField("medication.ndc", "ndc",
        ("ndc", "ndc number", "ndc #"),
        notes="CRITICAL: normalize to 11-digit 5-4-2."),
    CanonicalField("medication.strength", "text", ("strength", "dose", "dosage")),
    CanonicalField("medication.dosage_form", "text",
        ("dosage form", "form", "formulation")),
    CanonicalField("medication.route", "text",
        ("route", "route of administration", "roa")),
    CanonicalField("medication.sig", "text",
        ("directions", "sig", "instructions", "dosing", "dosing instructions")),
    CanonicalField("medication.quantity", "number",
        ("quantity", "qty", "quantity per fill")),
    CanonicalField("medication.days_supply", "number",
        ("days supply", "day supply", "supply")),
    CanonicalField("medication.refills", "number", ("refills", "# refills")),
    CanonicalField("medication.daw", "checkbox",
        ("daw", "dispense as written", "brand necessary")),
    CanonicalField("medication.hcpcs_jcode", "jcode",
        ("j-code", "jcode", "hcpcs", "hcpcs code"),
        notes="Medical (buy-and-bill) PA."),
    CanonicalField("medication.cpt_code", "cpt",
        ("cpt", "cpt code", "procedure code")),
    CanonicalField("medication.requested_start_date", "date",
        ("start date", "date of service", "service date", "requested start")),
    CanonicalField("medication.place_of_service", "text",
        ("place of service", "pos")),
    CanonicalField("medication.site_of_care", "text",
        ("site of care", "administration location", "where administered"),
        notes="home / office / infusion center."),

    # ---- CLINICAL / JUSTIFICATION ----------------------------------------
    CanonicalField("clinical.primary_diagnosis_code", "icd10",
        ("diagnosis", "icd-10", "icd10", "dx", "primary diagnosis", "diagnosis code",
         "icd code", "icd-10 code", "icd10 code", "principal diagnosis"),
        required=True, notes="CRITICAL: ICD-10 format validated."),
    CanonicalField("clinical.primary_diagnosis_description", "text",
        ("diagnosis description", "dx description")),
    CanonicalField("clinical.secondary_diagnoses", "icd10",
        ("secondary diagnosis", "other diagnoses", "additional diagnoses"),
        repeating=True),
    CanonicalField("clinical.date_of_diagnosis", "date",
        ("date of diagnosis", "onset date", "diagnosis date")),
    CanonicalField("clinical.relevant_lab_values", "text",
        ("lab results", "labs", "lab values", "relevant labs",
         "relevant laboratory", "relevant lab", "laboratory test",
         "lab test", "lab name", "test name"),
        repeating=True),
    CanonicalField("clinical.clinical_rationale", "text",
        ("clinical justification", "medical necessity", "rationale", "justification",
         "reason for request")),
    CanonicalField("clinical.prior_therapies", "list",
        ("previous medications", "step therapy", "tried and failed",
         "prior treatments", "medication history", "previous therapy"),
        repeating=True,
        notes="Each item: drug, start_date, end_date, outcome, reason_discontinued. "
              "The decisive PA field; model as structured list, not free text."),
    CanonicalField("clinical.contraindications", "text",
        ("contraindications", "reason cannot use preferred")),
    CanonicalField("clinical.step_therapy_completed", "checkbox",
        ("step therapy completed", "tried preferred", "step therapy met")),
    CanonicalField("clinical.date_of_last_treatment", "date",
        ("date of last treatment", "last treatment date")),
    CanonicalField("clinical.treatment_history_notes", "text",
        ("treatment history", "history notes")),

    # ---- REQUEST METADATA -------------------------------------------------
    CanonicalField("request.request_type", "enum",
        ("request type", "new/renewal", "initial or renewal", "type of request",
         "new therapy", "continuation of therapy", "new request",
         "initial request", "continuation", "renewal request", "renewal"),
        notes="new | renewal | continuation | expedited."),
    CanonicalField("request.is_expedited", "checkbox",
        ("urgent", "expedited", "stat", "expedited request")),
    CanonicalField("request.previous_auth_number", "text",
        ("previous auth #", "prior authorization number", "previous auth number"),
        notes="For renewals."),
    CanonicalField("request.date_of_request", "date",
        ("date", "request date", "today's date", "date of request")),
    CanonicalField("request.requested_duration", "text",
        ("duration", "length of therapy", "requested duration")),
    CanonicalField("request.signature", "signature",
        ("prescriber signature", "physician signature", "signature")),
    CanonicalField("request.signature_date", "date",
        ("date signed", "signature date")),
]


# Fields where a WRONG fill causes a denial -> never write on low confidence; defer.
CRITICAL_FIELDS = {
    "patient.last_name", "patient.dob",
    "insurance.member_id", "insurance.payer_name",
    "prescriber.npi",
    "medication.drug_name", "medication.ndc",
    "clinical.primary_diagnosis_code",
}


# ---------------------------------------------------------------------------
# Indexes + label resolver
# ---------------------------------------------------------------------------

BY_PATH = {f.path: f for f in CATALOG}

ALIAS_INDEX = {}
for _f in CATALOG:
    for _a in _f.aliases:
        ALIAS_INDEX.setdefault(_a, _f.path)

def _norm_label(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^\w\s/#-]", "", s)
    return re.sub(r"\s+", " ", s)

def resolve_label(form_label: str) -> Optional[str]:
    """Map a raw form label -> canonical path using most-specific-wins logic.

    1. Exact-normalized match (fastest, always correct).
    2. Word-boundary substring search: collect ALL alias hits and return the one
       with the longest alias.  This prevents short generic aliases like 'name'
       from overriding 'insurance' for a field like 'Primary Insurance Name'.
       Aligns with the same logic in pa_schema_extractor.classify_field().
    Returns None if no alias matches.
    """
    n = _norm_label(form_label)
    if not n:
        return None
    # --- exact match ---
    if n in ALIAS_INDEX:
        return ALIAS_INDEX[n]
    # --- most-specific substring match ---
    best: tuple | None = None   # (alias_len, path)
    for alias, path in ALIAS_INDEX.items():
        if alias in n or n in alias:
            if best is None or len(alias) > best[0]:
                best = (len(alias), path)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Pydantic container (what the API actually fills/serializes)
# ---------------------------------------------------------------------------

class PriorTherapy(BaseModel):
    drug: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    outcome: Optional[str] = None              # failed | intolerant | partial
    reason_discontinued: Optional[str] = None


class Patient(BaseModel):
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    dob: Optional[date] = None
    sex: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    ssn: Optional[str] = None
    weight_kg: Optional[float] = None
    height_cm: Optional[float] = None
    allergies: Optional[str] = None


class Insurance(BaseModel):
    payer_name: Optional[str] = None
    member_id: Optional[str] = None
    group_number: Optional[str] = None
    plan_name: Optional[str] = None
    rx_bin: Optional[str] = None
    rx_pcn: Optional[str] = None
    rx_group: Optional[str] = None
    subscriber_name: Optional[str] = None
    subscriber_relationship: Optional[str] = None
    secondary_payer_name: Optional[str] = None
    secondary_member_id: Optional[str] = None


class Prescriber(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    npi: Optional[str] = None
    dea: Optional[str] = None
    state_license: Optional[str] = None
    specialty: Optional[str] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    email: Optional[str] = None
    address_line1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    contact_name: Optional[str] = None


class Facility(BaseModel):
    name: Optional[str] = None
    npi: Optional[str] = None
    tax_id: Optional[str] = None
    ptan: Optional[str] = None
    address_line1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    phone: Optional[str] = None
    fax: Optional[str] = None


class Medication(BaseModel):
    drug_name: Optional[str] = None
    ndc: Optional[str] = None
    strength: Optional[str] = None
    dosage_form: Optional[str] = None
    route: Optional[str] = None
    sig: Optional[str] = None
    quantity: Optional[float] = None
    days_supply: Optional[int] = None
    refills: Optional[int] = None
    daw: Optional[bool] = None
    hcpcs_jcode: Optional[str] = None
    cpt_code: Optional[str] = None
    requested_start_date: Optional[date] = None
    place_of_service: Optional[str] = None
    site_of_care: Optional[str] = None


class Clinical(BaseModel):
    primary_diagnosis_code: Optional[str] = None
    primary_diagnosis_description: Optional[str] = None
    secondary_diagnoses: List[str] = Field(default_factory=list)
    date_of_diagnosis: Optional[date] = None
    relevant_lab_values: List[str] = Field(default_factory=list)
    clinical_rationale: Optional[str] = None
    prior_therapies: List[PriorTherapy] = Field(default_factory=list)
    contraindications: Optional[str] = None
    step_therapy_completed: Optional[bool] = None
    date_of_last_treatment: Optional[date] = None
    treatment_history_notes: Optional[str] = None


class RequestMeta(BaseModel):
    request_type: Optional[str] = None
    is_expedited: Optional[bool] = None
    previous_auth_number: Optional[str] = None
    date_of_request: Optional[date] = None
    requested_duration: Optional[str] = None
    signature: Optional[str] = None
    signature_date: Optional[date] = None


class PARequest(BaseModel):
    """Top-level canonical record for one prior-authorization request."""
    patient: Patient = Field(default_factory=Patient)
    insurance: Insurance = Field(default_factory=Insurance)
    prescriber: Prescriber = Field(default_factory=Prescriber)
    facility: Facility = Field(default_factory=Facility)
    medication: Medication = Field(default_factory=Medication)
    clinical: Clinical = Field(default_factory=Clinical)
    request: RequestMeta = Field(default_factory=RequestMeta)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from collections import Counter
    print(f"Total canonical fields: {len(CATALOG)}")
    by_entity = Counter(f.path.split('.')[0] for f in CATALOG)
    for ent, n in by_entity.items():
        print(f"  {ent:<12} {n} fields")
    print(f"Critical fields (defer if low-confidence): {len(CRITICAL_FIELDS)}")
    print(f"Sensitive/PHI fields (encrypt at rest): "
          f"{sum(f.sensitive for f in CATALOG)}")
    # quick resolver demo
    for label in ["Date of Birth", "Member #", "Provider NPI", "Tried and Failed",
                  "favorite color"]:
        print(f"  resolve({label!r}) -> {resolve_label(label)}")
