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
from typing import List, Optional, Tuple

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
    choices: tuple = ()             # allowed (value, label) options for enum types


# Shared US state options (abbreviation, full name) for the enum state fields.
US_STATES: tuple = (
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"),
    ("DC", "District of Columbia"), ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"),
    ("ID", "Idaho"), ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"),
    ("KS", "Kansas"), ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"),
    ("MD", "Maryland"), ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
    ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"),
    ("NV", "Nevada"), ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"),
    ("NY", "New York"), ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"),
    ("OK", "Oklahoma"), ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"),
    ("SC", "South Carolina"), ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"),
    ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"),
    ("WV", "West Virginia"), ("WI", "Wisconsin"), ("WY", "Wyoming"),
)
_US_STATE_CHOICES = tuple((abbr, f"{abbr} — {name}") for abbr, name in US_STATES)


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
        notes="Normalize to M / F / X / Unknown.",
        choices=(("M", "Male"), ("F", "Female"), ("X", "Other"),
                 ("Unknown", "Unknown"))),
    CanonicalField("patient.address_line1", "text",
        ("address", "street", "street address", "patient address", "address line 1"),
        sensitive=True),
    CanonicalField("patient.address_line2", "text",
        ("apt", "suite", "unit", "address line 2"), sensitive=True),
    CanonicalField("patient.city", "text", ("city",), sensitive=True),
    CanonicalField("patient.state", "enum", ("state", "st"),
        notes="Two-letter US state.", choices=_US_STATE_CHOICES),
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
    # Default / ordering clinician. Role-specific providers live under
    # requesting_provider / attending_provider / billing_provider so forms that
    # ask for more than one provider do not collapse onto this path.
    CanonicalField("prescriber.full_name", "text",
        ("prescriber", "prescriber name", "physician name", "provider name",
         "doctor", "md", "provider", "print", "printed name",
         "physician print", "prescriber print", "physician signature"),
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
        ("office phone", "provider phone", "prescriber phone", "contact phone")),
    CanonicalField("prescriber.phone_ext", "text",
        ("phone ext", "phone extension", "ext."),
        notes="Phone extension next to the office/provider phone. "
              "Avoid bare 'ext'/'extension' — too many false hits."),
    CanonicalField("prescriber.fax", "phone",
        ("fax", "provider fax", "office fax", "prescriber fax", "fax number"),
        required=True, notes="PA decisions still return by fax; usually mandatory."),
    CanonicalField("prescriber.email", "email", ("provider email", "prescriber email")),
    CanonicalField("prescriber.address_line1", "text",
        ("provider address", "prescriber address", "office address")),
    CanonicalField("prescriber.city", "text", ("provider city",)),
    CanonicalField("prescriber.state", "enum", ("provider state",),
        choices=_US_STATE_CHOICES),
    CanonicalField("prescriber.zip", "zip", ("provider zip",)),
    CanonicalField("prescriber.contact_name", "text",
        ("contact", "office contact", "contact person", "contact name"),
        notes="Staffer the payer calls back."),

    # ---- REQUESTING PROVIDER (OON / referral forms) -----------------------
    CanonicalField("requesting_provider.full_name", "text",
        ("requesting provider", "requesting physician", "requesting provider name",
         "ordering provider", "referring provider", "referring physician"),
        notes="Distinct from attending/billing when the form asks for several."),
    CanonicalField("requesting_provider.npi", "npi",
        ("requesting provider npi", "requesting npi", "ordering provider npi",
         "referring provider npi", "requesting provider npi/provider id")),
    CanonicalField("requesting_provider.phone", "phone",
        ("requesting provider phone", "requesting phone")),
    CanonicalField("requesting_provider.fax", "phone",
        ("requesting provider fax", "requesting fax")),
    CanonicalField("requesting_provider.specialty", "text",
        ("requesting provider specialty", "requesting specialty")),
    CanonicalField("requesting_provider.address_line1", "text",
        ("requesting provider address",)),
    CanonicalField("requesting_provider.city", "text", ("requesting provider city",)),
    CanonicalField("requesting_provider.state", "enum", ("requesting provider state",),
        choices=_US_STATE_CHOICES),
    CanonicalField("requesting_provider.zip", "zip", ("requesting provider zip",)),

    # ---- ATTENDING PROVIDER ----------------------------------------------
    CanonicalField("attending_provider.full_name", "text",
        ("attending provider", "attending physician", "attending provider name",
         "attending", "attending md")),
    CanonicalField("attending_provider.npi", "npi",
        ("attending provider npi", "attending npi")),
    CanonicalField("attending_provider.phone", "phone",
        ("attending provider phone", "attending phone")),
    CanonicalField("attending_provider.fax", "phone",
        ("attending provider fax", "attending fax")),
    CanonicalField("attending_provider.specialty", "text",
        ("attending provider specialty", "attending specialty")),
    CanonicalField("attending_provider.address_line1", "text",
        ("attending provider address",)),
    CanonicalField("attending_provider.city", "text", ("attending provider city",)),
    CanonicalField("attending_provider.state", "enum", ("attending provider state",),
        choices=_US_STATE_CHOICES),
    CanonicalField("attending_provider.zip", "zip", ("attending provider zip",)),

    # ---- BILLING PROVIDER ------------------------------------------------
    CanonicalField("billing_provider.full_name", "text",
        ("billing provider", "billing provider name", "billing physician",
         "billing md")),
    CanonicalField("billing_provider.npi", "npi",
        ("billing provider npi", "billing npi")),
    CanonicalField("billing_provider.phone", "phone",
        ("billing provider phone", "billing phone")),
    CanonicalField("billing_provider.fax", "phone",
        ("billing provider fax", "billing fax")),
    CanonicalField("billing_provider.tax_id", "tax_id",
        ("billing tin", "billing tax id", "billing provider tin"),
        sensitive=True),
    CanonicalField("billing_provider.address_line1", "text",
        ("billing provider address", "billing address")),
    CanonicalField("billing_provider.city", "text", ("billing provider city",)),
    CanonicalField("billing_provider.state", "enum", ("billing provider state",),
        choices=_US_STATE_CHOICES),
    CanonicalField("billing_provider.zip", "zip", ("billing provider zip",)),

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
    CanonicalField("facility.state", "enum", ("facility state",),
        choices=_US_STATE_CHOICES),
    CanonicalField("facility.zip", "zip", ("facility zip",)),
    CanonicalField("facility.phone", "phone", ("facility phone",)),
    CanonicalField("facility.fax", "phone", ("facility fax",)),

    # ---- PHARMACY --------------------------------------------------------
    CanonicalField("pharmacy.name", "text",
        ("pharmacy name", "pharmacy", "retail pharmacy", "dispensing pharmacy",
         "pharmacy / facility", "pharmacy name and phone"),
        notes="Dispensing pharmacy — not the patient or prescriber name."),
    CanonicalField("pharmacy.phone", "phone",
        ("pharmacy phone", "pharmacy telephone", "pharmacy #")),
    CanonicalField("pharmacy.fax", "phone", ("pharmacy fax",)),
    CanonicalField("pharmacy.npi", "npi",
        ("pharmacy npi", "ncpdp", "ncpdp number"),
        notes="Pharmacy NPI or NCPDP provider id when present."),
    CanonicalField("pharmacy.address_line1", "text",
        ("pharmacy address", "pharmacy street address")),
    CanonicalField("pharmacy.city", "text", ("pharmacy city",)),
    CanonicalField("pharmacy.state", "enum", ("pharmacy state",),
        choices=_US_STATE_CHOICES),
    CanonicalField("pharmacy.zip", "zip", ("pharmacy zip",)),
    CanonicalField("pharmacy.store_number", "text",
        ("store number", "pharmacy store number", "store #")),

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
    CanonicalField("medication.frequency", "text",
        ("frequency", "frequency and schedule", "dosing frequency",
         "administration frequency", "freq"),
        notes="Dosing cadence column, often separate from full sig on tables."),
    CanonicalField("medication.ingredient", "text",
        ("ingredient", "ingredient name", "compound ingredient", "active ingredient"),
        repeating=True,
        notes="Compound-drug component; repeats one row per ingredient."),
    CanonicalField("medication.quantity", "number",
        ("quantity", "qty", "quantity per fill")),
    CanonicalField("medication.days_supply", "number",
        ("days supply", "day supply", "supply")),
    CanonicalField("medication.refills", "number", ("refills", "# refills")),
    CanonicalField("medication.daw", "checkbox",
        ("daw", "dispense as written", "brand necessary")),
    CanonicalField("medication.hcpcs_jcode", "jcode",
        ("j-code", "jcode", "hcpcs", "hcpcs code", "billing code", "billing code / j code",
         "billing code/ j code"),
        notes="Medical (buy-and-bill) PA."),
    CanonicalField("medication.cpt_code", "cpt",
        ("cpt", "cpt code", "procedure code")),
    CanonicalField("medication.code_description", "text",
        ("code description", "procedure description", "service description",
         "description of service"),
        notes="Free-text description paired with a CPT/HCPCS/ICD code row."),
    CanonicalField("medication.requested_start_date", "date",
        ("start date", "date of service", "service date", "requested start")),
    CanonicalField("medication.place_of_service", "text",
        ("place of service", "pos")),
    CanonicalField("medication.site_of_care", "text",
        ("site of care", "administration location", "where administered"),
        notes="home / office / infusion center."),
    CanonicalField("medication.retail_price", "number",
        ("retail price", "enter the retail price", "billed amount",
         "charge amount"),
        notes="Billed/retail dollar amount on medical or claim-style PA forms."),

    # ---- CLINICAL / JUSTIFICATION ----------------------------------------
    CanonicalField("clinical.primary_diagnosis_code", "icd10",
        ("diagnosis", "icd-10", "icd10", "dx", "primary diagnosis", "diagnosis code",
         "icd code", "icd-10 code", "icd10 code", "principal diagnosis"),
        required=True, notes="CRITICAL: ICD-10 format validated."),
    CanonicalField("clinical.primary_diagnosis_description", "text",
        ("diagnosis description", "dx description")),
    CanonicalField("clinical.icd_version", "text",
        ("icd version", "icd-10 version", "icd10 version", "icd code set",
         "icd version number"),
        notes="ICD code set version label next to the diagnosis code."),
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
         "reason for request", "other explain", "other (explain",
         "other (specify")),
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
        ("treatment history", "history notes", "describe response",
         "reason for failure", "response reason for failure",
         "describe response reason for failure or allergy",
         "specific failure", "adverse event")),
    CanonicalField("clinical.comments", "text",
        ("comments", "additional comments", "additional comments if any",
         "opap comments", "remarks"),
        notes="Free-text comments box distinct from clinical rationale."),
    CanonicalField("clinical.therapy_type", "enum",
        ("physical therapy", "occupational therapy", "speech therapy",
         "cardiac rehab", "therapy type", "type of therapy"),
        notes="Rehab/therapy modality checkboxes. Each option often has its "
              "own AcroForm widget; mapping review targets this path. Full "
              "checkbox→value fill needs field→(path,value) later.",
        choices=(("PT", "Physical Therapy"), ("OT", "Occupational Therapy"),
                 ("ST", "Speech Therapy"), ("CR", "Cardiac Rehab"))),
    CanonicalField("clinical.functional_status", "text",
        ("functional status", "activities of daily living", "adl", "adls",
         "feeding", "bathing", "dressing", "dressing upper body",
         "dressing lower body", "toileting", "toilet/hygiene", "grooming",
         "transfer", "bed mobility", "wheelchair mobility", "ambulation",
         "gait", "mobility"),
        repeating=True,
        notes="SNF/therapy functional assessment items (ADLs). One row per "
              "activity; scored current/prior. Incremental cluster — extend as "
              "more rehab/therapy forms are reviewed."),
    CanonicalField("clinical.attachments", "checkbox",
        ("attachments", "supporting documents", "documents attached",
         "clinical documents attached")),

    # ---- REQUEST METADATA -------------------------------------------------
    CanonicalField("request.request_type", "enum",
        ("request type", "new/renewal", "initial or renewal", "type of request",
         "new therapy", "continuation of therapy", "new request",
         "initial request", "continuation", "renewal request", "renewal"),
        notes="new | renewal | continuation | expedited.",
        choices=(("new", "New"), ("renewal", "Renewal"),
                 ("continuation", "Continuation"), ("expedited", "Expedited"))),
    CanonicalField("request.is_expedited", "checkbox",
        ("urgent", "expedited", "stat", "expedited request", "exigent circumstances"),
        notes="Urgency / exigent-circumstances checkbox."),
    CanonicalField("request.previous_auth_number", "text",
        ("previous auth #", "prior authorization number", "previous auth number"),
        notes="For renewals."),
    CanonicalField("request.date_of_request", "date",
        ("date", "request date", "today's date", "date of request")),
    CanonicalField("request.requested_duration", "text",
        ("duration", "length of therapy", "requested duration")),
    CanonicalField("request.number_of_visits", "number",
        ("number of visits", "no of visits", "no of treatment visits",
         "visits authorized", "visit count", "# of visits"),
        notes="Therapy / outpatient visit count on medical PA forms."),
    CanonicalField("request.priority", "text",
        ("priority", "request priority", "priority level")),
    CanonicalField("request.signature", "signature",
        ("prescriber signature", "physician signature", "signature")),
    CanonicalField("request.signature_date", "date",
        ("date signed", "signature date")),

    # ---- CLAIM / AUDIT (appears on some imported medical PA packs) --------
    CanonicalField("request.claim_audit_number", "text",
        ("claim audit number", "audit number", "unique identifier",
         "claim number", "claim #"),
        notes="Claim/audit identifier on medical review / overpayment forms."),
    CanonicalField("request.overpayment_reason", "text",
        ("overpayment reason", "reason for overpayment",
         "reason for denial", "denial reason"),
        notes="Payer-side / audit fields that ride along some blank packs."),
    CanonicalField("request.refund_amount", "number",
        ("refund amount", "overpayment amount"),
        notes="Dollar amount on overpayment / refund audit forms."),
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

# path -> ((value, label), ...) for enum fields that declare allowed options.
CATALOG_CHOICES = {f.path: f.choices for f in CATALOG if f.choices}

ALIAS_INDEX = {}
for _f in CATALOG:
    for _a in _f.aliases:
        ALIAS_INDEX.setdefault(_a, _f.path)

def _norm_label(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^\w\s/#-]", "", s)
    return re.sub(r"\s+", " ", s)


# Generic "entity / section / role" words that are aliases only for the coarse
# ``*.full_name`` / ``*.payer_name`` fields.  On their own they are weak signals
# ("Patient", "Provider", "Information", "Name") — a *specific* token in the same
# label ("DOB", "NPI", "Fax", "City") should always win over them.  They are only
# used as a fallback when no specific alias matches (so "Primary Insurance Name"
# still resolves to insurance.payer_name via the longest weak alias).
WEAK_ALIASES = {
    "patient", "member", "subscriber", "insured",
    "physician", "prescriber", "provider", "doctor",
    "insurance", "facility", "requestor", "requester",
    "pharmacy",
    "name", "information", "info", "primary", "secondary",
}

# When the *only* hit is the weak alias ``name`` → patient.full_name, reject if
# the label clearly names another entity (Pharmacy name, Transplant organ, …).
_WEAK_NAME_BLOCKERS = re.compile(
    r"\b(pharmacy|transplant|organ|facility|clinic|hospital|store|"
    r"drug|medication|diagnosis|procedure|service|tin|npi)\b",
    re.I,
)

# Minimum length for a *partial* (whole-word) alias hit.  1–2 char aliases
# ("st", "mi", "ht", "wt", "dx", "md") are landmines inside real words and are
# only honoured as an *exact* full-label match.
_MIN_PARTIAL_ALIAS_LEN = 3


def resolve_label_conf(form_label: str) -> Tuple[Optional[str], Optional[str]]:
    """Map a raw form label -> ``(canonical_path, confidence)``.

    Tightened "Stage 1" dictionary matcher.  It is deliberately conservative so
    that ambiguous / noisy labels defer to Gemini instead of producing a wrong
    (and possibly silent) canonical mapping.

    Ladder:
      1. **Exact** normalized alias match  → ``(path, "high")``.
      2. **Whole-word partial** match (``\\b alias s? \\b``, alias ≥ 3 chars):
         collect every hit, then
           - prefer *specific* aliases over generic entity/section words
             (see :data:`WEAK_ALIASES`), and among the chosen tier take the
             longest alias.  → ``(path, "medium")``.
           - if the longest alias is tied across two *different* canonical paths
             the label is genuinely ambiguous → ``(None, None)`` (defer to AI).
      3. No hit → ``(None, None)``.

    The word-boundary (rather than raw substring) match is what kills the classic
    false positives — "st" inside "atteST"/"hiSTory", "bin" inside "prescriBINg",
    "form" inside "inFORMation" — while the optional trailing ``s`` keeps plurals
    and possessives ("provider's", "physicians").
    """
    n = _norm_label(form_label)
    if not n:
        return (None, None)

    # 1. exact normalized alias — always trustworthy
    if n in ALIAS_INDEX:
        return (ALIAS_INDEX[n], "high")

    # 2. whole-word partial hits (with optional plural 's')
    hits = [
        (alias, path)
        for alias, path in ALIAS_INDEX.items()
        if len(alias) >= _MIN_PARTIAL_ALIAS_LEN
        and re.search(r"\b" + re.escape(alias) + r"s?\b", n)
    ]
    if not hits:
        return (None, None)

    # Specific aliases outrank generic entity/section words.
    strong = [(a, p) for a, p in hits if a not in WEAK_ALIASES]
    pool = strong or hits

    # Bare "name" → patient.full_name must not win on "Pharmacy name" etc.
    if not strong and _WEAK_NAME_BLOCKERS.search(n):
        pool = [(a, p) for a, p in pool if a != "name"]
        if not pool:
            return (None, None)

    best_len = max(len(a) for a, _ in pool)
    top_paths = {p for a, p in pool if len(a) == best_len}
    if len(top_paths) > 1:
        # Longest alias tied across different canonical paths -> ambiguous.
        return (None, None)

    return (next(iter(top_paths)), "medium")


def resolve_label(form_label: str) -> Optional[str]:
    """Backward-compatible wrapper returning only the canonical path (or None).

    Prefer :func:`resolve_label_conf` when the caller can use the confidence
    tier (e.g. to defer *critical* medium-confidence fields to the AI pass).
    """
    return resolve_label_conf(form_label)[0]


# Section headings that tell patient vs prescriber apart when the printed
# caption is generic ("Name", "Address", "Phone", "City").
_SECTION_PRESCRIBER_RE = re.compile(
    r"\b(prescriber|provider|physician|ordering\s+provider|requesting\s+physician|"
    r"doctor|md\b|npi\b)\b",
    re.I,
)
_SECTION_PATIENT_RE = re.compile(
    r"\b(patient|member|subscriber|insured)\b",
    re.I,
)
_SECTION_PHARMACY_RE = re.compile(r"\bpharmacy\b", re.I)
# Identity / contact leaves that share the same short labels on both parties.
_ENTITY_SWAPPABLE = frozenset({
    "full_name", "first_name", "last_name", "middle_name",
    "address_line1", "address_line2", "city", "state", "zip",
    "phone", "phone_ext", "fax", "email",
    "npi", "specialty", "tax_id",
})

# Role cues in the *label* (stronger than section) for multi-provider forms.
_LABEL_ROLE_RES = (
    (re.compile(
        r"\b(requesting(\s+provider|\s+physician)?|ordering\s+provider|"
        r"referring(\s+provider|\s+physician)?)\b", re.I
    ), "requesting_provider"),
    (re.compile(r"\battending(\s+provider|\s+physician|\s+md)?\b", re.I),
     "attending_provider"),
    (re.compile(r"\bbilling(\s+provider|\s+physician|\s+md)?\b", re.I),
     "billing_provider"),
    (re.compile(r"\bpharmacy\b", re.I), "pharmacy"),
)

_PROVIDER_ROOTS = frozenset({
    "prescriber", "requesting_provider", "attending_provider", "billing_provider",
})


def infer_entity_from_section(section: Optional[str]) -> Optional[str]:
    """Return entity root (``prescriber``, ``patient``, ``pharmacy``, …) or None."""
    s = (section or "").strip()
    if not s:
        return None
    if _SECTION_PHARMACY_RE.search(s):
        return "pharmacy"
    # Prescriber wins when both words appear ("Patient's Prescriber …" rare).
    if _SECTION_PRESCRIBER_RE.search(s):
        return "prescriber"
    if _SECTION_PATIENT_RE.search(s):
        return "patient"
    return None


def infer_role_from_label(label: Optional[str]) -> Optional[str]:
    """Return a role entity from printed caption (requesting / attending / …)."""
    s = (label or "").strip()
    if not s:
        return None
    for cre, entity in _LABEL_ROLE_RES:
        if cre.search(s):
            return entity
    return None


def apply_section_to_path(
    path: Optional[str],
    section: Optional[str],
) -> Optional[str]:
    """Remap ``patient.*`` ↔ ``prescriber.*`` / ``pharmacy.*`` using section.

    Bare aliases like ``Address`` / ``Name`` / ``Phone`` resolve to patient.*
    with high confidence; Section 4 Prescriber Information must flip those to
    ``prescriber.*``. Only swappable identity/contact leaves are moved — NPI
    without a role cue stays unless the leaf exists on the target entity.
    """
    if not path or path == "other" or "." not in path:
        return path
    entity = infer_entity_from_section(section)
    if not entity:
        return path
    root, leaf = path.split(".", 1)
    if leaf not in _ENTITY_SWAPPABLE:
        return path
    if root == entity:
        return path
    if root in ("patient", "prescriber", "pharmacy") and entity in (
        "patient", "prescriber", "pharmacy"
    ):
        candidate = f"{entity}.{leaf}"
        if candidate in BY_PATH:
            return candidate
        # pharmacy.full_name does not exist — use pharmacy.name
        if entity == "pharmacy" and leaf == "full_name":
            if "pharmacy.name" in BY_PATH:
                return "pharmacy.name"
    return path


def apply_label_role_to_path(
    path: Optional[str],
    label: Optional[str],
    section: Optional[str] = None,
) -> Optional[str]:
    """Remap generic patient/prescriber paths using role words in the label.

    ``Requesting provider`` / ``Attending provider`` / ``Billing provider`` /
    ``Pharmacy name`` must not all collapse to ``prescriber.full_name`` or
    ``patient.full_name``. Prefer role cues in the caption; fall back to
    :func:`apply_section_to_path`.
    """
    if not path or path == "other" or "." not in path:
        return path
    root, leaf = path.split(".", 1)
    role = infer_role_from_label(label)
    if role and leaf in _ENTITY_SWAPPABLE | {"name"}:
        if role == "pharmacy" and leaf in ("full_name", "name"):
            candidate = "pharmacy.name"
        else:
            candidate = f"{role}.{leaf}"
        if candidate in BY_PATH:
            return candidate
    if root in ("patient", "prescriber", "pharmacy") or root in _PROVIDER_ROOTS:
        return apply_section_to_path(path, section)
    return path


# ---------------------------------------------------------------------------
# Checkbox / radio option value helpers  (field → (path, value))
# ---------------------------------------------------------------------------
# A mapping entry may carry an optional ``value``: the canonical choice this
# widget means when checked (e.g. patient.sex + "M", clinical.therapy_type +
# "PT"). Fill only ticks the widget when the user's data for that path matches.

# Generic AcroForm on-states — never useful as enum choice hints.
_GENERIC_EXPORTS = frozenset({"on", "yes", "off", "true", "false", "1", "0", "x"})


def infer_option_value(path: str, *hints: Optional[str]) -> Optional[str]:
    """Pick a catalog choice value for ``path`` from label/export hints.

    Returns the choice's *value* (e.g. ``"M"``, ``"PT"``) or None when the
    path has no choices or no hint matches. Used at map-build time so
    checkbox/radio widgets get ``{canonical, value}`` without a human edit.
    """
    cf = BY_PATH.get(path)
    if not cf or not getattr(cf, "choices", ()):
        return None
    for hint in hints:
        if not hint:
            continue
        raw = str(hint).strip()
        tail = raw.split("/")[-1].strip()
        for candidate in (raw, tail):
            cl = candidate.lower()
            if not cl or cl in _GENERIC_EXPORTS:
                continue
            for val, lab in cf.choices:
                if cl == str(val).lower() or cl == str(lab).lower():
                    return str(val)
            # Contains-match only for longer candidates — short tokens like
            # "on"/"ot" falsely hit "Occupational" / "Continuation".
            if len(cl) < 4:
                continue
            for val, lab in cf.choices:
                ll = str(lab).lower()
                if len(ll) >= 4 and (ll in cl or cl in ll):
                    return str(val)
    return None


def option_values_match(path: str, user_value, option_value: str) -> bool:
    """True if ``user_value`` selects the same catalog choice as ``option_value``.

    Treats choice value and label as equivalent (``"M"`` == ``"Male"``) so
    guided-fill enums and free-text inputs both light the right checkbox.
    """
    if user_value in (None, "") or option_value in (None, ""):
        return False
    u = str(user_value).strip().lower()
    o = str(option_value).strip().lower()
    if u == o:
        return True
    cf = BY_PATH.get(path)
    if not cf or not getattr(cf, "choices", ()):
        return False
    for val, lab in cf.choices:
        aliases = {str(val).lower(), str(lab).lower()}
        if u in aliases and o in aliases:
            return True
    return False


def map_field_key(field: dict) -> str:
    """Stable cache/review key for one AcroForm widget.

    Plain field name for ordinary widgets. For radio-group options (same
    parent name, distinct export values) returns ``name::export`` so each
    option keeps its own ``{canonical, value}`` mapping — matching extract's
    per-widget rows without breaking the fill writer (which strips ``::``).
    """
    name = str(field.get("name") or "")
    if field.get("_radio_group") and field.get("export_value"):
        return f"{name}::{field['export_value']}"
    return name


def acro_field_name(map_key: str) -> str:
    """AcroForm ``/T`` name from a :func:`map_field_key` (strips ``::export``)."""
    if not map_key:
        return ""
    return map_key.split("::", 1)[0]


def map_key_export(map_key: str) -> Optional[str]:
    """Export value encoded in ``name::export``, or None."""
    if not map_key or "::" not in map_key:
        return None
    return map_key.split("::", 1)[1] or None


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
    frequency: Optional[str] = None
    ingredient: Optional[str] = None
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
    icd_version: Optional[str] = None
    secondary_diagnoses: List[str] = Field(default_factory=list)
    date_of_diagnosis: Optional[date] = None
    relevant_lab_values: List[str] = Field(default_factory=list)
    clinical_rationale: Optional[str] = None
    prior_therapies: List[PriorTherapy] = Field(default_factory=list)
    contraindications: Optional[str] = None
    step_therapy_completed: Optional[bool] = None
    date_of_last_treatment: Optional[date] = None
    treatment_history_notes: Optional[str] = None
    functional_status: List[str] = Field(default_factory=list)


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
