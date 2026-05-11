"""
Input Adapter
=============
Accepts user data in ANY shape — legacy flat dict, camelCase, nested canonical,
CSV row, etc. — and produces a rich dict the VisionService AI prompt can use:

  1. parse()           Try to coerce raw input into a CanonicalRecord.
                       Unknown keys fall into custom{} (no data loss).

  2. derive()          Compute derived fields:
                         full_name ← first + last + credentials
                         city_state_zip ← address parts
                         primary_icd_code ← diagnoses[primary=True]
                         etc.

  3. gate_questions()  Drop QuestionAnswer entries where applicable=False or
                       whose section doesn't match form_context.indication.
                       (Prevents Botox chronic-migraine answers landing in the
                       OAB section just because labels sound similar.)

  4. flatten()         Recursively flatten the nested record into a flat dict.
                       One canonical value emits MULTIPLE synonym keys so the
                       AI gets several matching shots regardless of form vocabulary.

  5. to_ai_input()     Public entry point. Returns a dict with two keys:
                         "flat"       → flat {str: str} with all aliases
                         "structured" → JSON-serialised CanonicalRecord (for
                                        disambiguation by the AI prompt)

Usage:
    from fillmypdf.services.input_adapter import InputAdapter

    adapted = InputAdapter().to_ai_input(raw_user_dict)
    # pass adapted["flat"] as user_data to VisionService
    # pass adapted["structured"] as extra context in the prompt
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ..models.canonical import (
    Address,
    CanonicalRecord,
    Contact,
    Diagnosis,
    DateRange,
    FormContext,
    MedicalInfo,
    Medication,
    Organization,
    Person,
    Procedure,
    Provider,
    QuestionAnswer,
    Signature,
)


# ---------------------------------------------------------------------------
# Alias table
# Each entry maps a canonical flat key to a list of synonyms.
# When a value is emitted for the canonical key, ALL synonyms get the same value.
# ---------------------------------------------------------------------------

ALIASES: Dict[str, List[str]] = {
    # --- patient / member / primary ---
    "patient_name":       ["enrollees_name", "members_name", "name", "full_name",
                           "patient_full_name", "member_name", "insured_name"],
    "patient_first_name": ["first_name", "enrollee_first_name"],
    "patient_last_name":  ["last_name", "enrollee_last_name"],
    "patient_dob":        ["dob", "date_of_birth", "birthdate", "birth_date",
                           "enrollee_dob", "member_dob"],
    "patient_id":         ["member_id", "enrollee_id", "plan_id", "policy_number",
                           "patient_member_id", "enrollees_member_id"],
    "patient_ssn":        ["ssn", "social_security", "taxpayer_id"],
    "patient_phone":      ["phone", "patient_phone_number", "enrollee_phone"],
    "patient_address":    ["address", "home_address", "street_address"],
    "patient_city":       ["city", "patient_city"],
    "patient_state":      ["state", "patient_state"],
    "patient_zip":        ["zip", "zip_code", "postal_code"],
    "patient_city_state_zip": ["city_state_zip"],

    # --- prescriber / physician / provider ---
    "physician_name":     ["prescriber_name", "provider_name", "doctor_name",
                           "prescribers_name", "name_and_npi", "prescriber_name_npi"],
    "physician_npi":      ["npi", "prescriber_npi", "provider_npi", "name_and_npi_number"],
    "physician_phone":    ["office_phone", "prescriber_phone", "provider_phone",
                           "doctor_phone", "physician_office_phone"],
    "physician_fax":      ["fax", "prescriber_fax", "provider_fax"],
    "physician_address":  ["prescriber_address", "provider_address"],
    "physician_city_state_zip": ["prescriber_city_state_zip"],

    # --- facility / organization ---
    "facility_name":      ["provider_facility_name", "facility", "clinic_name"],
    "facility_npi":       ["provider_facility_npi", "facility_npi"],

    # --- medical / drug ---
    "drug_name":          ["medication_name", "medication_j_code", "drug_requested",
                           "medication", "drug"],
    "drug_j_code":        ["j_code", "medication_j_code"],
    "drug_strength":      ["strength", "dose", "strength_units", "units_per_dose",
                           "strength_units_per_dose"],
    "drug_frequency":     ["frequency", "frequency_of_administration",
                           "frequency_per_dose"],
    "drug_sig":           ["directions", "directions_for_administration",
                           "sig", "dosing_instructions"],
    "drug_quantity":      ["quantity", "qty", "qty_fill", "qty_per_fill",
                           "quantity_per_fill"],
    "benefit_type":       ["pharmacy_or_medical_benefit", "benefit",
                           "drug_obtained_via"],

    # --- diagnosis ---
    "diagnosis":          ["diagnosis_description", "primary_diagnosis"],
    "icd_code":           ["icd_10_code", "icd10_code", "icd_code_1",
                           "primary_icd_code", "diagnosis_code"],
    "icd_code_2":         ["icd_10_code_2", "secondary_icd_code"],

    # --- service dates ---
    "dates_of_service":   ["service_dates", "date_of_service"],
    "service_start_date": ["start_date_of_service"],
    "service_end_date":   ["end_date_of_service"],

    # --- cpt codes ---
    "cpt_code_1":         ["cpt_1", "cpt_code", "procedure_code_1"],
    "cpt_code_2":         ["cpt_2", "procedure_code_2"],

    # --- clinical ---
    "clinical_notes":     ["additional_clinical_info", "additional_information",
                           "comments", "additional_notes", "clinical_information"],
    "request_type":       ["prior_auth_type", "pa_request_type"],
    "indication":         ["indication_for_use", "diagnosis_indication"],

    # --- prior therapy ---
    "prior_therapy_1":    ["previously_tried_drug_1", "failed_medication_1"],
    "prior_therapy_2":    ["previously_tried_drug_2", "failed_medication_2"],

    # --- signatures ---
    "signature_date":     ["date_of_signature", "prescriber_signature_date", "date"],
    "prescriber_signature": ["signature", "prescriber_sign"],

    # --- tax ---
    "filing_status":      ["tax_filing_status", "withholding_status"],
}

# Reverse index: synonym → canonical key (for incoming data normalisation)
_SYNONYM_TO_CANONICAL: Dict[str, str] = {
    syn: canon
    for canon, syns in ALIASES.items()
    for syn in syns
}


# ---------------------------------------------------------------------------
# Value normalisers
# ---------------------------------------------------------------------------

def _normalise_phone(raw: str) -> str:
    """Strip non-digits and format as (xxx) xxx-xxxx if 10 digits."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        d = digits[1:]
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return raw


def _normalise_date(raw: str) -> str:
    """Try common date patterns and normalise to MM/DD/YYYY."""
    raw = raw.strip()
    # Already MM/DD/YYYY
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", raw):
        return raw
    # YYYY-MM-DD (ISO)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
    # DD-MM-YYYY
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", raw)
    if m:
        return f"{m.group(2)}/{m.group(1)}/{m.group(3)}"
    return raw


def _normalise_bool(raw: Any) -> str:
    """Coerce boolean-ish values → 'Yes' / 'No'."""
    if isinstance(raw, bool):
        return "Yes" if raw else "No"
    s = str(raw).strip().lower()
    if s in ("true", "yes", "y", "1", "x"):
        return "Yes"
    if s in ("false", "no", "n", "0"):
        return "No"
    return str(raw)


def _flatten_dict(d: Dict, prefix: str = "", sep: str = "__") -> Dict[str, str]:
    """Recursively flatten a nested dict into a flat {str: str} dict."""
    out: Dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}{sep}{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten_dict(v, key, sep))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out.update(_flatten_dict(item, f"{key}_{i+1}", sep))
                elif item is not None:
                    out[f"{key}_{i+1}"] = str(item)
        elif v is not None:
            out[key] = str(v)
    return out


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------

class InputAdapter:
    """
    Convert any user-supplied data dict into a rich AI input bundle.
    """

    # ------------------------------------------------------------------
    # Step 1 — Parse
    # ------------------------------------------------------------------

    def parse(self, raw: Dict[str, Any]) -> CanonicalRecord:
        """
        Try to coerce raw dict into a CanonicalRecord.
        Supports three input shapes:
          (a) Already canonical/nested (has keys like 'primary', 'provider', 'medical')
          (b) Flat snake_case (has keys like 'patient_name', 'physician_phone')
          (c) Flat camelCase (patientName, physicianPhone)
        Unknown keys land in custom{} — no data loss.
        """
        # Detect if already nested
        nested_keys = {"primary", "provider", "medical", "tax", "insurance",
                       "employment", "facility", "organization", "questions",
                       "signatures", "form_context"}
        if nested_keys & set(raw.keys()):
            try:
                return CanonicalRecord(**raw)
            except Exception:
                pass  # fall through to flat promotion

        # Flat → promote to canonical nested shape
        return self._promote_flat(raw)

    def _promote_flat(self, raw: Dict[str, Any]) -> CanonicalRecord:
        """Promote a flat dict to a CanonicalRecord by best-effort key matching."""
        # Normalise keys: camelCase → snake_case
        flat = {self._to_snake(k): v for k, v in raw.items()}

        # Resolve synonyms to canonical keys
        flat = {_SYNONYM_TO_CANONICAL.get(k, k): v for k, v in flat.items()}

        def g(k):
            v = flat.get(k)
            return str(v).strip() if v is not None else None

        # --- primary person ---
        primary = Person(
            first_name=g("patient_first_name"),
            last_name=g("patient_last_name"),
            full_name=g("patient_name"),
            dob=g("patient_dob"),
            ssn=g("patient_ssn"),
            contact=Contact(phone=g("patient_phone")),
            address=Address(
                line1=g("patient_address"),
                city=g("patient_city"),
                state=g("patient_state"),
                postal_code=g("patient_zip"),
            ),
            identifiers={k: v for k, v in {
                "member_id": g("patient_id"),
            }.items() if v},
        )

        # --- provider ---
        provider = Provider(
            full_name=g("physician_name"),
            npi=g("physician_npi"),
            contact=Contact(
                phone=g("physician_phone"),
                fax=g("physician_fax"),
            ),
            address=Address(
                line1=g("physician_address"),
                city=g("physician_city"),
                state=g("physician_state"),
                postal_code=g("physician_zip"),
            ),
        )

        # --- facility ---
        facility = Organization(
            name=g("facility_name"),
            npi=g("facility_npi"),
        )

        # --- medical ---
        diagnoses = []
        if g("icd_code"):
            diagnoses.append(Diagnosis(
                code=g("icd_code"),
                description=g("diagnosis"),
                primary=True,
            ))
        if g("icd_code_2"):
            diagnoses.append(Diagnosis(code=g("icd_code_2")))

        medications = []
        if g("drug_name"):
            medications.append(Medication(
                name=g("drug_name"),
                j_code=g("drug_j_code"),
                strength=g("drug_strength"),
                frequency=g("drug_frequency"),
                quantity=g("drug_quantity"),
                sig=g("drug_sig"),
            ))

        procedures = []
        for i in range(1, 5):
            c = g(f"cpt_code_{i}")
            if c:
                procedures.append(Procedure(cpt_code=c))

        svc_dates = None
        if g("dates_of_service"):
            svc_dates = DateRange(start=g("dates_of_service"))
        elif g("service_start_date") or g("service_end_date"):
            svc_dates = DateRange(
                start=g("service_start_date"),
                end=g("service_end_date"),
            )

        medical = MedicalInfo(
            diagnoses=diagnoses,
            medications=medications,
            procedures=procedures,
            service_dates=svc_dates,
            benefit_type=g("benefit_type"),
            request_type=g("request_type"),
            indication=g("indication"),
            clinical_notes=g("clinical_notes"),
        )

        # --- questions ---
        # A key is a question if any of:
        #   - starts with "question_" / "q_"
        #   - matches "q<digit>..."
        #   - contains "__" (section__name format we emit) and doesn't end in "__notes"
        questions: Dict[str, QuestionAnswer] = {}
        question_prefixes = ("question_", "q_")
        for k, v in flat.items():
            is_q = (
                any(k.startswith(p) for p in question_prefixes)
                or bool(re.match(r"^q\d", k))
                or ("__" in k and not k.endswith("__notes"))
            )
            if is_q:
                # Detect section prefix (key format: section__question_name)
                section = None
                qkey = k
                if "__" in k:
                    section, qkey = k.split("__", 1)
                questions[qkey] = QuestionAnswer(
                    value=v,
                    section=section,
                )

        # --- signatures ---
        signatures = []
        if g("signature_date") or g("prescriber_signature"):
            signatures.append(Signature(
                signer_role="prescriber",
                signer_name=g("physician_name"),
                date=g("signature_date"),
                signature_text=g("prescriber_signature"),
            ))

        # --- form_context ---
        form_ctx = FormContext(
            indication=g("indication"),
            request_type=g("request_type"),
        )

        # --- leftover → custom ---
        known = {
            "patient_name", "patient_first_name", "patient_last_name", "patient_dob",
            "patient_ssn", "patient_phone", "patient_address", "patient_city",
            "patient_state", "patient_zip", "patient_id",
            "physician_name", "physician_npi", "physician_phone", "physician_fax",
            "physician_address", "physician_city", "physician_state", "physician_zip",
            "facility_name", "facility_npi",
            "icd_code", "icd_code_2", "diagnosis",
            "drug_name", "drug_j_code", "drug_strength", "drug_frequency",
            "drug_quantity", "drug_sig",
            "cpt_code_1", "cpt_code_2", "cpt_code_3", "cpt_code_4",
            "dates_of_service", "service_start_date", "service_end_date",
            "benefit_type", "request_type", "indication", "clinical_notes",
            "signature_date", "prescriber_signature",
        }
        def _is_question_key(k: str) -> bool:
            return (
                any(k.startswith(p) for p in question_prefixes)
                or bool(re.match(r"^q\d", k))
                or ("__" in k and not k.endswith("__notes"))
            )

        custom = {k: v for k, v in flat.items()
                  if k not in known and not _is_question_key(k)}

        return CanonicalRecord(
            primary=primary,
            provider=provider,
            facility=facility,
            medical=medical,
            questions=questions,
            signatures=signatures,
            form_context=form_ctx,
            custom=custom,
        )

    @staticmethod
    def _to_snake(name: str) -> str:
        """Convert camelCase / PascalCase to snake_case."""
        s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()

    # ------------------------------------------------------------------
    # Step 2 — Derive computed fields
    # ------------------------------------------------------------------

    def derive(self, record: CanonicalRecord) -> CanonicalRecord:
        """Fill in derived / convenience fields in-place."""
        # full_name already derived by Person's model_validator
        # Derive city_state_zip on address objects (stored in custom for flattenability)
        for role, person in [("primary", record.primary), ("provider", record.provider)]:
            if person and person.address:
                csz = person.address.city_state_zip
                if csz:
                    person.custom[f"{role}_city_state_zip"] = csz

        # Primary diagnosis shortcut
        if record.medical and record.medical.diagnoses:
            for d in record.medical.diagnoses:
                if d.primary:
                    record.medical.custom["primary_icd_code"] = d.code
                    if d.description:
                        record.medical.custom["primary_diagnosis"] = d.description
                    break

        # Medication shortcut
        if record.medical and record.medical.medications:
            m = record.medical.medications[0]
            record.medical.custom["primary_drug_name"] = m.name
            if m.sig:
                record.medical.custom["primary_drug_sig"] = m.sig

        # Service dates display string
        if record.medical and record.medical.service_dates:
            disp = record.medical.service_dates.display
            if disp:
                record.medical.custom["service_dates_display"] = disp

        # Signature date shortcut
        if record.signatures:
            sig = record.signatures[0]
            if sig.date:
                record.custom["signature_date"] = sig.date

        return record

    # ------------------------------------------------------------------
    # Step 3 — Gate questions by indication
    # ------------------------------------------------------------------

    def gate_questions(self, record: CanonicalRecord) -> CanonicalRecord:
        """
        Drop QuestionAnswer entries that are:
          (a) marked applicable=False, or
          (b) belong to a section that doesn't match form_context.indication
              (only when indication is set AND the Q has a section set).
        """
        indication = None
        if record.form_context and record.form_context.indication:
            indication = record.form_context.indication.lower().replace(" ", "_")
        elif record.medical and record.medical.indication:
            indication = record.medical.indication.lower().replace(" ", "_")

        filtered = {}
        for key, qa in record.questions.items():
            if not qa.applicable:
                continue
            if indication and qa.section:
                # Keep if section starts with indication or is "universal" / "continuation"
                sec = qa.section.lower()
                always_keep = sec in ("universal", "continuation", "continuation_request")
                if not always_keep and not sec.startswith(indication):
                    continue
            filtered[key] = qa

        record.questions = filtered
        return record

    # ------------------------------------------------------------------
    # Step 4 — Flatten with alias emission
    # ------------------------------------------------------------------

    def flatten(self, record: CanonicalRecord) -> Dict[str, str]:
        """
        Produce a flat {str: str} dict with ALL synonym keys emitted.
        Values are string-normalised (dates, phones, booleans).
        """
        flat: Dict[str, str] = {}

        def put(key: str, value: Any):
            if value is None:
                return
            val = self._normalise_value(key, value)
            if not val:
                return
            flat[key] = val
            for syn in ALIASES.get(key, []):
                flat[syn] = val

        # --- primary person ---
        p = record.primary
        if p:
            put("patient_name", p.full_name)
            put("patient_first_name", p.first_name)
            put("patient_last_name", p.last_name)
            put("patient_dob", p.dob)
            put("patient_ssn", p.ssn)
            for idk, idv in (p.identifiers or {}).items():
                put(f"patient_{idk}", idv)
                if idk == "member_id":
                    put("patient_id", idv)
            if p.contact:
                put("patient_phone", p.contact.phone)
            if p.address:
                put("patient_address", p.address.line1)
                put("patient_city", p.address.city)
                put("patient_state", p.address.state)
                put("patient_zip", p.address.postal_code)
                csz = p.address.city_state_zip
                if csz:
                    put("patient_city_state_zip", csz)
            for k, v in (p.custom or {}).items():
                put(k, v)

        # --- provider ---
        pr = record.provider
        if pr:
            put("physician_name", pr.full_name)
            put("physician_npi", pr.npi)
            if pr.contact:
                put("physician_phone", pr.contact.phone)
                put("physician_fax", pr.contact.fax)
            if pr.address:
                put("physician_address", pr.address.line1)
                csz = pr.address.city_state_zip
                if csz:
                    put("physician_city_state_zip", csz)
            for k, v in (pr.custom or {}).items():
                put(k, v)

        # --- facility ---
        f = record.facility
        if f:
            put("facility_name", f.name)
            put("facility_npi", f.npi)

        # --- medical ---
        med = record.medical
        if med:
            put("benefit_type", med.benefit_type)
            put("request_type", med.request_type)
            put("indication", med.indication)
            put("clinical_notes", med.clinical_notes)
            if med.service_dates:
                d = med.service_dates.display or med.service_dates.start
                put("dates_of_service", d)
            for i, dx in enumerate(med.diagnoses, 1):
                suffix = "" if i == 1 else f"_{i}"
                put(f"icd_code{suffix}", dx.code)
                if dx.description:
                    put(f"diagnosis{suffix}", dx.description)
            for i, rx in enumerate(med.medications, 1):
                suffix = "" if i == 1 else f"_{i}"
                put(f"drug_name{suffix}", rx.name)
                put(f"drug_j_code{suffix}", rx.j_code)
                put(f"drug_strength{suffix}", rx.strength)
                put(f"drug_frequency{suffix}", rx.frequency)
                put(f"drug_quantity{suffix}", rx.quantity)
                put(f"drug_sig{suffix}", rx.sig)
            for i, proc in enumerate(med.procedures, 1):
                put(f"cpt_code_{i}", proc.cpt_code)
            for k, v in (med.custom or {}).items():
                put(k, v)

        # --- questions ---
        for qkey, qa in record.questions.items():
            # Emit with section prefix (for disambiguation)
            prefixed = f"{qa.section}__{qkey}" if qa.section else qkey
            put(prefixed, _normalise_bool(qa.value))
            put(qkey, _normalise_bool(qa.value))
            if qa.notes:
                put(f"{qkey}__notes", qa.notes)

        # --- signatures ---
        for sig in record.signatures:
            put("signature_date", sig.date)
            if sig.signature_text:
                put("prescriber_signature", sig.signature_text)
            if sig.signer_name:
                put(f"{sig.signer_role}_signature_name", sig.signer_name)

        # --- custom (top-level escape hatch) ---
        for k, v in (record.custom or {}).items():
            put(k, v)

        return flat

    def _normalise_value(self, key: str, value: Any) -> str:
        """Normalise a value based on key hints."""
        if value is None:
            return ""
        raw = str(value).strip()
        if not raw:
            return ""

        # Phone keys
        if any(w in key for w in ("phone", "fax", "tel")):
            return _normalise_phone(raw)

        # Date keys
        if any(w in key for w in ("dob", "date", "_date", "birthdate", "service_date")):
            return _normalise_date(raw)

        # Boolean keys
        if isinstance(value, bool):
            return _normalise_bool(value)

        return raw

    # ------------------------------------------------------------------
    # Step 5 — Public entry point
    # ------------------------------------------------------------------

    def to_ai_input(
        self,
        raw: Dict[str, Any],
        base: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Full pipeline: raw dict → AI-ready input bundle.

        Returns:
            {
              "flat": {str: str},   # all keys + aliases — feed to AI prompt
              "structured": str,    # JSON of CanonicalRecord — disambiguation context
            }
        """
        # Merge base (profile) data under record (record wins)
        merged = {**(base or {}), **raw}

        record = self.parse(merged)
        record = self.derive(record)
        record = self.gate_questions(record)
        flat = self.flatten(record)

        # Structured view for prompt disambiguation (serialise to JSON string)
        structured = record.model_dump(exclude_none=True, exclude_defaults=True)

        return {
            "flat": flat,
            "structured": structured,
        }
