"""
Unit tests for fillmypdf.models.canonical
=========================================
Validates the master template:
  • Pydantic validation (required vs optional fields, enums)
  • Derived field computation (Person.full_name, Address.city_state_zip)
  • Composability (each domain payload independent)
  • Escape hatches (custom dict at every level)
"""

import pytest
from pydantic import ValidationError

from fillmypdf.models.canonical import (
    Address,
    CanonicalRecord,
    Contact,
    DateRange,
    Diagnosis,
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
# Address
# ---------------------------------------------------------------------------

class TestAddress:
    def test_empty_address_is_valid(self):
        addr = Address()
        assert addr.country == "US"
        assert addr.city_state_zip is None

    def test_city_state_zip_derivation_full(self):
        addr = Address(city="Austin", state="TX", postal_code="78756")
        assert addr.city_state_zip == "Austin, TX, 78756"

    def test_city_state_zip_partial(self):
        addr = Address(city="Austin")
        assert addr.city_state_zip == "Austin"

    def test_custom_dict_accepts_arbitrary(self):
        addr = Address(custom={"unit": "Suite 310", "landmark": "Capitol"})
        assert addr.custom["unit"] == "Suite 310"


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------

class TestContact:
    def test_empty_contact(self):
        c = Contact()
        assert c.phone is None and c.fax is None and c.email is None

    def test_all_fields(self):
        c = Contact(phone="555-1234", phone_alt="555-5678",
                    fax="555-9999", email="a@b.com")
        assert c.phone == "555-1234"
        assert c.email == "a@b.com"


# ---------------------------------------------------------------------------
# Person
# ---------------------------------------------------------------------------

class TestPerson:
    def test_full_name_derived_from_first_last(self):
        p = Person(first_name="Sarah", last_name="Johnson")
        assert p.full_name == "Sarah Johnson"

    def test_full_name_derived_with_title_credentials(self):
        p = Person(title="Dr.", first_name="Sarah",
                   last_name="Johnson", credentials="MD")
        assert p.full_name == "Dr. Sarah Johnson MD"

    def test_explicit_full_name_not_overridden(self):
        p = Person(first_name="Sarah", last_name="Johnson",
                   full_name="S. Johnson")
        assert p.full_name == "S. Johnson"

    def test_no_name_inputs_means_no_full_name(self):
        p = Person(dob="1990-01-01")
        assert p.full_name is None

    def test_identifiers_arbitrary(self):
        p = Person(identifiers={"member_id": "M1", "mrn": "MRN42",
                                "medicare_id": "MED99"})
        assert len(p.identifiers) == 3

    def test_invalid_gender_rejected(self):
        with pytest.raises(ValidationError):
            Person(gender="INVALID")  # type: ignore[arg-type]

    def test_valid_gender_accepted(self):
        for g in ["M", "F", "X", "U"]:
            assert Person(gender=g).gender == g


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class TestProvider:
    def test_provider_is_a_person(self):
        p = Provider(first_name="James", last_name="Patel",
                     credentials="MD", npi="1234567890")
        assert p.full_name == "James Patel MD"
        assert p.npi == "1234567890"

    def test_provider_specific_fields(self):
        p = Provider(first_name="Jane", last_name="Doe",
                     dea="BD1234567", license_number="TX12345",
                     specialty="Cardiology")
        assert p.dea == "BD1234567"
        assert p.specialty == "Cardiology"


# ---------------------------------------------------------------------------
# Medical
# ---------------------------------------------------------------------------

class TestDiagnosis:
    def test_required_code(self):
        with pytest.raises(ValidationError):
            Diagnosis()  # type: ignore[call-arg]

    def test_default_icd10(self):
        d = Diagnosis(code="K59.04")
        assert d.code_system == "ICD10"
        assert d.primary is False

    def test_invalid_code_system(self):
        with pytest.raises(ValidationError):
            Diagnosis(code="X", code_system="ICD11")  # type: ignore[arg-type]


class TestMedication:
    def test_required_name(self):
        with pytest.raises(ValidationError):
            Medication()  # type: ignore[call-arg]

    def test_optional_fields(self):
        m = Medication(name="Linzess", strength="145mcg",
                       frequency="once daily", days_supply=30)
        assert m.days_supply == 30
        assert m.refills is None


class TestMedicalInfo:
    def test_empty_medical(self):
        m = MedicalInfo()
        assert m.diagnoses == []
        assert m.medications == []
        assert m.indication is None

    def test_full_medical_payload(self):
        m = MedicalInfo(
            diagnoses=[Diagnosis(code="G43.701", primary=True)],
            medications=[Medication(name="Botox", j_code="J0585")],
            procedures=[Procedure(cpt_code="64615")],
            service_dates=DateRange(start="05/15/2026", end="05/15/2027"),
            request_type="initial",
            indication="chronic_migraine",
        )
        assert m.diagnoses[0].primary
        assert m.indication == "chronic_migraine"

    def test_invalid_request_type(self):
        with pytest.raises(ValidationError):
            MedicalInfo(request_type="brand-new")  # type: ignore[arg-type]

    def test_invalid_benefit_type(self):
        with pytest.raises(ValidationError):
            MedicalInfo(benefit_type="cash")  # type: ignore[arg-type]


class TestDateRange:
    def test_display_with_both(self):
        dr = DateRange(start="01/01/2026", end="06/30/2026")
        assert dr.display == "01/01/2026 – 06/30/2026"

    def test_display_with_start_only(self):
        dr = DateRange(start="01/01/2026")
        assert dr.display == "01/01/2026"

    def test_display_with_neither(self):
        assert DateRange().display is None


# ---------------------------------------------------------------------------
# Question bag
# ---------------------------------------------------------------------------

class TestQuestionAnswer:
    def test_bool_value(self):
        q = QuestionAnswer(value=True, section="universal")
        assert q.value is True
        assert q.applicable is True   # default

    def test_string_value(self):
        q = QuestionAnswer(value="Propranolol, Topiramate")
        assert q.value == "Propranolol, Topiramate"

    def test_inapplicable_marker(self):
        q = QuestionAnswer(value=True, applicable=False)
        assert q.applicable is False


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

class TestSignature:
    def test_required_role(self):
        with pytest.raises(ValidationError):
            Signature()  # type: ignore[call-arg]

    def test_valid_roles(self):
        for role in ["primary", "prescriber", "guardian", "witness", "agent", "employer"]:
            assert Signature(signer_role=role).signer_role == role

    def test_invalid_role(self):
        with pytest.raises(ValidationError):
            Signature(signer_role="customer")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

class TestCanonicalRecord:
    def test_empty_record_is_valid(self):
        r = CanonicalRecord()
        assert r.primary is None
        assert r.questions == {}
        assert r.dependents == []

    def test_record_with_just_primary(self):
        r = CanonicalRecord(primary=Person(first_name="A", last_name="B"))
        assert r.primary.full_name == "A B"
        assert r.medical is None  # composability — domain payloads optional

    def test_record_composition_medical_only(self):
        r = CanonicalRecord(medical=MedicalInfo(indication="chronic_migraine"))
        assert r.medical.indication == "chronic_migraine"
        assert r.tax is None
        assert r.insurance is None

    def test_record_with_all_domains(self):
        """Master template covers all domains in one record."""
        r = CanonicalRecord(
            primary=Person(first_name="X", last_name="Y"),
            provider=Provider(npi="123"),
            medical=MedicalInfo(),
            form_context=FormContext(form_name="Test"),
            questions={"q1": QuestionAnswer(value=True)},
            signatures=[Signature(signer_role="prescriber")],
        )
        assert r.primary is not None
        assert r.provider is not None
        assert r.medical is not None
        assert "q1" in r.questions

    def test_custom_escape_hatch(self):
        """Anything not in the schema goes in custom — never lost."""
        r = CanonicalRecord(custom={"weird_field": "value", "another": 42})
        assert r.custom["weird_field"] == "value"
        assert r.custom["another"] == 42

    def test_organisation_with_identifiers(self):
        org = Organization(name="Acme Health",
                           identifiers={"tin": "12-3456789", "internal": "X42"})
        assert org.name == "Acme Health"
        assert len(org.identifiers) == 2
