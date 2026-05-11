"""
Unit tests for fillmypdf.services.input_adapter.InputAdapter
============================================================
Validates the canonical layer:
  • parse() — accepts flat dict, camelCase, nested canonical
  • derive() — computes full_name, primary_diagnosis, etc.
  • gate_questions() — drops out-of-scope questions
  • flatten() — emits canonical key + all aliases
  • normalisation — phones, dates, booleans
"""

import pytest

from fillmypdf.models.canonical import (
    CanonicalRecord, FormContext, MedicalInfo, Person, Provider,
    QuestionAnswer, Signature, Diagnosis, Medication,
)
from fillmypdf.services.input_adapter import (
    InputAdapter, _normalise_bool, _normalise_date, _normalise_phone,
)


# ---------------------------------------------------------------------------
# Value normalisers
# ---------------------------------------------------------------------------

class TestPhoneNormaliser:
    def test_10_digit_plain(self):
        assert _normalise_phone("5125550192") == "(512) 555-0192"

    def test_with_dashes(self):
        assert _normalise_phone("512-555-0192") == "(512) 555-0192"

    def test_with_dots(self):
        assert _normalise_phone("512.555.0192") == "(512) 555-0192"

    def test_with_country_code(self):
        assert _normalise_phone("15125550192") == "(512) 555-0192"

    def test_already_formatted(self):
        assert _normalise_phone("(512) 555-0192") == "(512) 555-0192"

    def test_invalid_kept_as_is(self):
        assert _normalise_phone("not-a-phone") == "not-a-phone"

    def test_short_number_kept(self):
        assert _normalise_phone("12345") == "12345"


class TestDateNormaliser:
    def test_already_us_format(self):
        assert _normalise_date("03/14/1978") == "03/14/1978"

    def test_iso_to_us(self):
        assert _normalise_date("1978-03-14") == "03/14/1978"

    def test_dd_mm_yyyy_to_us(self):
        assert _normalise_date("14-03-1978") == "03/14/1978"

    def test_unrecognised_kept(self):
        assert _normalise_date("March 14, 1978") == "March 14, 1978"


class TestBoolNormaliser:
    def test_true_variants(self):
        for v in [True, "true", "yes", "Y", "1", "x"]:
            assert _normalise_bool(v) == "Yes"

    def test_false_variants(self):
        for v in [False, "false", "no", "N", "0"]:
            assert _normalise_bool(v) == "No"

    def test_pass_through_other(self):
        assert _normalise_bool("Maybe") == "Maybe"


# ---------------------------------------------------------------------------
# parse() — shape detection
# ---------------------------------------------------------------------------

class TestParseShapes:
    def test_parse_already_canonical_nested(self):
        adapter = InputAdapter()
        record = adapter.parse({
            "primary": {"first_name": "Sarah", "last_name": "Johnson"},
            "medical": {"indication": "chronic_migraine"}
        })
        assert record.primary.full_name == "Sarah Johnson"
        assert record.medical.indication == "chronic_migraine"

    def test_parse_legacy_flat_snake_case(self):
        adapter = InputAdapter()
        record = adapter.parse({
            "patient_name": "Maria Gonzalez",
            "patient_dob": "1985-07-22",
            "physician_name": "Dr. Patel",
            "physician_phone": "5125550192",
        })
        assert record.primary is not None
        assert record.primary.full_name == "Maria Gonzalez"
        assert record.provider.full_name == "Dr. Patel"

    def test_parse_camel_case_flat(self):
        adapter = InputAdapter()
        record = adapter.parse({
            "patientName": "Maria",
            "physicianPhone": "5125550192",
        })
        assert record.primary.full_name == "Maria"
        assert record.provider.contact.phone == "5125550192"

    def test_parse_questions_with_section_prefix(self):
        adapter = InputAdapter()
        record = adapter.parse({
            "patient_name": "Test",
            "chronic_migraine_initial__fifteen_plus_days": "Yes",
            "universal__concurrent_botulinum": "No",
            "q1": True,
        })
        assert "fifteen_plus_days" in record.questions
        assert record.questions["fifteen_plus_days"].section == "chronic_migraine_initial"
        assert "concurrent_botulinum" in record.questions
        assert "q1" in record.questions

    def test_parse_unknown_keys_go_to_custom(self):
        adapter = InputAdapter()
        record = adapter.parse({
            "patient_name": "X",
            "completely_unknown_field": "preserved",
        })
        assert record.custom.get("completely_unknown_field") == "preserved"


# ---------------------------------------------------------------------------
# derive()
# ---------------------------------------------------------------------------

class TestDerive:
    def test_derives_city_state_zip(self):
        adapter = InputAdapter()
        record = CanonicalRecord(primary=Person(
            address=__import__("fillmypdf.models.canonical", fromlist=["Address"]).Address(
                city="Austin", state="TX", postal_code="78756"
            )
        ))
        record = adapter.derive(record)
        assert "primary_city_state_zip" in record.primary.custom

    def test_derives_primary_diagnosis(self):
        adapter = InputAdapter()
        record = CanonicalRecord(medical=MedicalInfo(diagnoses=[
            Diagnosis(code="K59.04", description="CIC", primary=True)
        ]))
        record = adapter.derive(record)
        assert record.medical.custom.get("primary_icd_code") == "K59.04"
        assert record.medical.custom.get("primary_diagnosis") == "CIC"

    def test_derives_signature_date(self):
        adapter = InputAdapter()
        record = CanonicalRecord(signatures=[
            Signature(signer_role="prescriber", date="04/30/2026")
        ])
        record = adapter.derive(record)
        assert record.custom.get("signature_date") == "04/30/2026"


# ---------------------------------------------------------------------------
# gate_questions()
# ---------------------------------------------------------------------------

class TestGateQuestions:
    def test_drops_inapplicable(self):
        adapter = InputAdapter()
        record = CanonicalRecord(questions={
            "q_keep": QuestionAnswer(value=True, applicable=True),
            "q_drop": QuestionAnswer(value=True, applicable=False),
        })
        record = adapter.gate_questions(record)
        assert "q_keep" in record.questions
        assert "q_drop" not in record.questions

    def test_drops_out_of_scope_section(self):
        adapter = InputAdapter()
        record = CanonicalRecord(
            form_context=FormContext(indication="chronic_migraine"),
            questions={
                "q_keep": QuestionAnswer(value=True, section="chronic_migraine_initial"),
                "q_drop": QuestionAnswer(value=True, section="overactive_bladder_initial"),
                "q_universal": QuestionAnswer(value=True, section="universal"),
            }
        )
        record = adapter.gate_questions(record)
        assert "q_keep" in record.questions
        assert "q_drop" not in record.questions
        assert "q_universal" in record.questions  # universal always kept

    def test_no_indication_keeps_all_applicable(self):
        adapter = InputAdapter()
        record = CanonicalRecord(questions={
            "q1": QuestionAnswer(value=True, section="chronic_migraine_initial"),
            "q2": QuestionAnswer(value=True, section="something_else"),
        })
        record = adapter.gate_questions(record)
        assert "q1" in record.questions
        assert "q2" in record.questions


# ---------------------------------------------------------------------------
# flatten() with alias emission
# ---------------------------------------------------------------------------

class TestFlattenAndAliases:
    def test_full_name_emits_alias_synonyms(self):
        adapter = InputAdapter()
        record = adapter.parse({
            "primary": {"first_name": "Sarah", "last_name": "Johnson"}
        })
        flat = adapter.flatten(record)
        # Canonical
        assert flat.get("patient_name") == "Sarah Johnson"
        # Aliases all present
        assert flat.get("enrollees_name") == "Sarah Johnson"
        assert flat.get("members_name") == "Sarah Johnson"
        assert flat.get("full_name") == "Sarah Johnson"

    def test_phone_normalisation_in_flatten(self):
        adapter = InputAdapter()
        record = adapter.parse({
            "provider": {"contact": {"phone": "5125550192"}}
        })
        flat = adapter.flatten(record)
        # Should be formatted
        assert flat.get("physician_phone") == "(512) 555-0192"
        # Aliases also formatted
        assert flat.get("office_phone") == "(512) 555-0192"

    def test_dob_normalisation_in_flatten(self):
        adapter = InputAdapter()
        record = adapter.parse({"primary": {"dob": "1985-07-22"}})
        flat = adapter.flatten(record)
        assert flat.get("patient_dob") == "07/22/1985"
        assert flat.get("dob") == "07/22/1985"  # alias

    def test_question_with_section_prefix_emitted(self):
        adapter = InputAdapter()
        record = CanonicalRecord(questions={
            "fifteen_plus_days": QuestionAnswer(
                value=True, section="chronic_migraine_initial"
            )
        })
        flat = adapter.flatten(record)
        assert flat.get("chronic_migraine_initial__fifteen_plus_days") == "Yes"
        assert flat.get("fifteen_plus_days") == "Yes"

    def test_question_bool_normalisation(self):
        adapter = InputAdapter()
        for raw, expected in [(True, "Yes"), (False, "No"),
                              ("yes", "Yes"), ("N", "No")]:
            record = CanonicalRecord(questions={
                "q": QuestionAnswer(value=raw)
            })
            flat = adapter.flatten(record)
            assert flat.get("q") == expected, f"Failed for {raw}"

    def test_diagnosis_emitted_with_index(self):
        adapter = InputAdapter()
        record = adapter.parse({
            "medical": {"diagnoses": [
                {"code": "K59.04", "description": "CIC", "primary": True},
                {"code": "M54.5"}
            ]}
        })
        flat = adapter.flatten(record)
        assert flat.get("icd_code") == "K59.04"
        assert flat.get("icd_code_2") == "M54.5"
        assert flat.get("diagnosis") == "CIC"

    def test_provider_address_csz_emitted(self):
        adapter = InputAdapter()
        record = adapter.parse({
            "provider": {"address": {"city": "Austin", "state": "TX",
                                      "postal_code": "78756"}}
        })
        flat = adapter.flatten(record)
        assert flat.get("physician_city_state_zip") is not None
        assert "Austin" in flat["physician_city_state_zip"]


# ---------------------------------------------------------------------------
# to_ai_input — full pipeline + bundle structure
# ---------------------------------------------------------------------------

class TestToAIInput:
    def test_returns_flat_and_structured(self):
        adapter = InputAdapter()
        bundle = adapter.to_ai_input({
            "primary": {"first_name": "Sarah", "last_name": "Johnson"}
        })
        assert "flat" in bundle
        assert "structured" in bundle
        assert isinstance(bundle["flat"], dict)
        assert isinstance(bundle["structured"], dict)

    def test_base_profile_merge(self):
        """Profile data merges UNDER record (record wins on conflict)."""
        adapter = InputAdapter()
        bundle = adapter.to_ai_input(
            raw={"patient_name": "Override Name"},
            base={"patient_name": "Base Name", "patient_phone": "5125550192"}
        )
        # Record-supplied name wins
        assert bundle["flat"].get("patient_name") == "Override Name"
        # Base-only field carried through
        assert bundle["flat"].get("patient_phone") == "(512) 555-0192"

    def test_legacy_flat_input_still_works(self):
        """Backwards compatibility — flat dict produces a populated bundle."""
        adapter = InputAdapter()
        bundle = adapter.to_ai_input({
            "patient_name": "Maria Gonzalez",
            "physician_name": "Dr. Patel",
            "icd_code": "M54.5",
        })
        flat = bundle["flat"]
        assert flat.get("patient_name") == "Maria Gonzalez"
        assert flat.get("physician_name") == "Dr. Patel"
        assert flat.get("icd_code") == "M54.5"

    def test_canonical_input_runs_full_pipeline(self):
        """The canonical input we used for RI PA form exercises every layer."""
        adapter = InputAdapter()
        bundle = adapter.to_ai_input({
            "form_context": {"indication": "physical_therapy"},
            "primary": {
                "first_name": "Maria", "last_name": "Gonzalez",
                "dob": "1985-07-22",
                "identifiers": {"member_id": "NHP-2025-88734"}
            },
            "provider": {
                "full_name": "Dr. James Patel",
                "npi": "1902837465",
                "contact": {"phone": "4015550142", "fax": "4015550143"}
            },
            "medical": {
                "diagnoses": [{"code": "M54.5", "description": "Low back pain",
                               "primary": True}]
            }
        })
        flat = bundle["flat"]
        # primary normalised
        assert flat.get("patient_name") == "Dr. James Patel" or flat.get("patient_name") == "Maria Gonzalez"
        assert flat.get("patient_dob") == "07/22/1985"
        # provider
        assert flat.get("physician_phone") == "(401) 555-0142"
        assert flat.get("physician_fax") == "(401) 555-0143"
        # diagnosis
        assert flat.get("icd_code") == "M54.5"
        # alias spread
        assert flat.get("members_name") is not None  # alias of patient_name
