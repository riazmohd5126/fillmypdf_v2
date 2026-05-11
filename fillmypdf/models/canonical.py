"""
Canonical Input Models
======================
A form-agnostic data schema that accepts patient/prescriber/medical data
in any shape and produces a normalised, flat dict the AI can map to any PDF.

Design principles:
  • Everything is Optional — required-ness is form-specific, not enforced here.
  • Nested by entity (Person, Provider, Organization) so the AI can disambiguate
    e.g. "patient phone" vs "prescriber phone" from structural context.
  • questions dict handles arbitrary Y/N grids (e.g. Botox PA has ~45 Q/A fields).
  • custom dict at every level catches anything not covered by the schema.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, Union
from datetime import date

from pydantic import BaseModel, EmailStr, Field, model_validator


# ---------------------------------------------------------------------------
# Address / Contact
# ---------------------------------------------------------------------------

class Address(BaseModel):
    line1: Optional[str] = None
    line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None          # "TX" or "Texas"
    postal_code: Optional[str] = None
    country: str = "US"
    custom: Dict[str, Any] = Field(default_factory=dict)

    @property
    def city_state_zip(self) -> Optional[str]:
        parts = [p for p in [self.city, self.state, self.postal_code] if p]
        return ", ".join(parts) if parts else None


class Contact(BaseModel):
    phone: Optional[str] = None
    phone_alt: Optional[str] = None
    fax: Optional[str] = None
    email: Optional[str] = None
    custom: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Persons
# ---------------------------------------------------------------------------

class Person(BaseModel):
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None        # derived if first+last given
    suffix: Optional[str] = None           # Jr., III …
    title: Optional[str] = None            # Dr., Mr., …
    credentials: Optional[str] = None      # MD, DO, NP, RN …

    dob: Optional[str] = None              # stored as string, normalised to MM/DD/YYYY
    ssn: Optional[str] = None
    gender: Optional[Literal["M", "F", "X", "U"]] = None
    marital_status: Optional[Literal["single", "married", "mfs", "hoh", "qw"]] = None

    identifiers: Dict[str, str] = Field(default_factory=dict)   # member_id, mrn, policy, …

    address: Optional[Address] = None
    contact: Optional[Contact] = None
    relationship_to_primary: Optional[str] = None
    custom: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def derive_full_name(self) -> "Person":
        if not self.full_name and (self.first_name or self.last_name):
            parts = [p for p in [
                self.title, self.first_name, self.middle_name,
                self.last_name, self.suffix, self.credentials
            ] if p]
            self.full_name = " ".join(parts)
        return self


class Provider(Person):
    """Physician / prescriber / advisor."""
    npi: Optional[str] = None
    dea: Optional[str] = None
    tax_id: Optional[str] = None
    license_number: Optional[str] = None
    specialty: Optional[str] = None


class Organization(BaseModel):
    name: Optional[str] = None
    tax_id: Optional[str] = None           # EIN
    npi: Optional[str] = None
    identifiers: Dict[str, str] = Field(default_factory=dict)
    address: Optional[Address] = None
    contact: Optional[Contact] = None
    custom: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Medical domain
# ---------------------------------------------------------------------------

class Diagnosis(BaseModel):
    code: str
    code_system: Literal["ICD10", "ICD9", "SNOMED"] = "ICD10"
    description: Optional[str] = None
    onset_date: Optional[str] = None
    primary: bool = False


class Medication(BaseModel):
    name: str
    j_code: Optional[str] = None
    ndc: Optional[str] = None
    strength: Optional[str] = None         # "145 mcg"
    dose: Optional[str] = None             # "1 capsule"
    frequency: Optional[str] = None        # "once daily"
    route: Optional[str] = None
    quantity: Optional[str] = None
    days_supply: Optional[int] = None
    refills: Optional[int] = None
    sig: Optional[str] = None              # full directions


class Procedure(BaseModel):
    cpt_code: str
    description: Optional[str] = None
    date_of_service: Optional[str] = None


class PriorTherapy(BaseModel):
    medication: str
    duration: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    outcome: Optional[Literal["failed", "intolerant", "contraindicated", "ineffective"]] = None
    notes: Optional[str] = None


class DateRange(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None

    @property
    def display(self) -> Optional[str]:
        parts = [p for p in [self.start, self.end] if p]
        return " – ".join(parts) if parts else None


class MedicalInfo(BaseModel):
    diagnoses: List[Diagnosis] = Field(default_factory=list)
    medications: List[Medication] = Field(default_factory=list)
    procedures: List[Procedure] = Field(default_factory=list)
    service_dates: Optional[DateRange] = None
    benefit_type: Optional[Literal["pharmacy", "medical", "dme", "both"]] = None

    request_type: Optional[Literal["initial", "renewal", "continuation", "appeal", "urgent"]] = None
    indication: Optional[str] = None       # "chronic_migraine", "overactive_bladder" …
    clinical_notes: Optional[str] = None

    prior_therapies: List[PriorTherapy] = Field(default_factory=list)
    contraindications: List[str] = Field(default_factory=list)
    allergies: List[str] = Field(default_factory=list)
    custom: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Other domain payloads
# ---------------------------------------------------------------------------

class TaxInfo(BaseModel):
    tax_year: Optional[int] = None
    filing_status: Optional[str] = None
    claim_dependents_amount: Optional[str] = None
    other_income: Optional[str] = None
    deductions: Optional[str] = None
    extra_withholding: Optional[str] = None
    multiple_jobs: Optional[bool] = None
    exempt: Optional[bool] = None
    custom: Dict[str, Any] = Field(default_factory=dict)


class InsuranceInfo(BaseModel):
    carrier: Optional[str] = None
    plan_name: Optional[str] = None
    policy_number: Optional[str] = None
    group_number: Optional[str] = None
    subscriber: Optional[Person] = None
    effective_date: Optional[str] = None
    termination_date: Optional[str] = None
    coverage_type: Optional[Literal["primary", "secondary", "tertiary"]] = None
    custom: Dict[str, Any] = Field(default_factory=dict)


class EmploymentInfo(BaseModel):
    employer: Optional[Organization] = None
    job_title: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    employment_type: Optional[Literal["full_time", "part_time", "contract", "intern"]] = None
    income: Optional[str] = None
    pay_frequency: Optional[Literal["hourly", "weekly", "biweekly", "monthly", "annual"]] = None
    custom: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Question bag  (handles arbitrary Y/N grids like Botox ~45 questions)
# ---------------------------------------------------------------------------

class QuestionAnswer(BaseModel):
    value: Union[bool, str]                  # True/False or free text
    section: Optional[str] = None            # "chronic_migraine_initial"
    notes: Optional[str] = None              # "please list medications" field
    applicable: bool = True                  # False → skip / leave blank


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

class Signature(BaseModel):
    signer_role: Literal[
        "primary", "prescriber", "guardian", "witness", "agent", "employer"
    ]
    signer_name: Optional[str] = None
    date: Optional[str] = None
    signature_text: Optional[str] = None     # typed name or "/s/ Jane Doe"


# ---------------------------------------------------------------------------
# Form context  (optional discriminators / hints)
# ---------------------------------------------------------------------------

class FormContext(BaseModel):
    form_name: Optional[str] = None
    form_version: Optional[str] = None
    indication: Optional[str] = None         # enables scope-gating of questions
    request_type: Optional[str] = None
    language: str = "en"
    submission_date: Optional[str] = None


# ---------------------------------------------------------------------------
# Root record
# ---------------------------------------------------------------------------

class CanonicalRecord(BaseModel):
    # People
    primary: Optional[Person] = None         # patient / employee / taxpayer
    secondary: Optional[Person] = None       # spouse / dependent / guarantor
    dependents: List[Person] = Field(default_factory=list)

    # Other parties
    provider: Optional[Provider] = None      # physician / prescriber
    organization: Optional[Organization] = None  # employer / insurer
    facility: Optional[Organization] = None  # clinic / hospital

    # Domain payloads (composable — only include what applies)
    medical: Optional[MedicalInfo] = None
    tax: Optional[TaxInfo] = None
    insurance: Optional[InsuranceInfo] = None
    employment: Optional[EmploymentInfo] = None

    # Question bag
    questions: Dict[str, QuestionAnswer] = Field(default_factory=dict)

    # Signatures
    signatures: List[Signature] = Field(default_factory=list)

    # Form context (optional — helps scope gating)
    form_context: Optional[FormContext] = None

    # Escape hatch
    custom: Dict[str, Any] = Field(default_factory=dict)
