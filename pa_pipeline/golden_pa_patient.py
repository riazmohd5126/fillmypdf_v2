"""
golden_pa_patient.py
====================
One fully-populated synthetic PARequest for eval runs.
All values are fictional — use for testing only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running standalone or imported from the repo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fillmypdf.models.pa_canonical import (
    PARequest, Patient, Insurance, Prescriber, Facility,
    Medication, Clinical, RequestMeta, PriorTherapy,
)
from datetime import date


GOLDEN_PATIENT = PARequest(
    patient=Patient(
        first_name="Jane",
        middle_name="A",
        last_name="Testpatient",
        full_name="Jane A Testpatient",
        dob=date(1975, 3, 22),
        sex="F",
        address_line1="123 Maple Street",
        address_line2="Apt 4B",
        city="Springfield",
        state="IL",
        zip="62701",
        phone="2175550123",
        email="jane.testpatient@example.com",
        weight_kg=68.0,
        height_cm=165.0,
        allergies="Penicillin",
    ),
    insurance=Insurance(
        payer_name="Molina Healthcare",
        member_id="MOL987654321",
        group_number="GRP-12345",
        plan_name="Molina Medicaid IL",
        rx_bin="610591",
        rx_pcn="MEDADVPC",
        rx_group="MOLINATEST",
        subscriber_name="Jane A Testpatient",
        subscriber_relationship="Self",
    ),
    prescriber=Prescriber(
        first_name="Robert",
        last_name="Testprescriber",
        full_name="Dr. Robert Testprescriber MD",
        npi="1234567893",
        dea="BT1234563",
        state_license="IL-MD-987654",
        specialty="Gastroenterology",
        phone="2175559876",
        fax="2175559877",
        email="r.testprescriber@gastroclinic-example.com",
        address_line1="456 Medical Plaza",
        city="Springfield",
        state="IL",
        zip="62702",
        contact_name="Mary Office",
    ),
    facility=Facility(
        name="Springfield GI Associates",
        npi="1487654321",
        tax_id="363636363",
        address_line1="456 Medical Plaza",
        city="Springfield",
        state="IL",
        zip="62702",
        phone="2175550456",
        fax="2175550457",
    ),
    medication=Medication(
        drug_name="Linzess",
        ndc="00074-3799-90",
        strength="290 mcg",
        dosage_form="Capsule",
        route="Oral",
        sig="Take 1 capsule by mouth once daily on an empty stomach 30 min before first meal",
        quantity=30,
        days_supply=30,
        refills=5,
        daw=False,
        hcpcs_jcode="",
        cpt_code="99213",
        requested_start_date=date(2026, 7, 1),
        place_of_service="11",
        site_of_care="Office",
    ),
    clinical=Clinical(
        primary_diagnosis_code="K58.9",
        primary_diagnosis_description="Irritable bowel syndrome without diarrhea",
        secondary_diagnoses=["K21.0", "Z87.39"],
        date_of_diagnosis=date(2024, 11, 15),
        relevant_lab_values=[
            "Colonoscopy (2024-10-01): No structural abnormalities",
            "Lactulose breath test (2024-11-01): Negative for SIBO",
        ],
        clinical_rationale=(
            "Patient has chronic IBS-C with inadequate response to dietary modifications "
            "and osmotic laxatives over 6 months. Linzess (linaclotide) is indicated per "
            "FDA approval for IBS-C and represents appropriate next-step therapy."
        ),
        prior_therapies=[
            PriorTherapy(
                drug="MiraLAX (polyethylene glycol 3350)",
                start_date=date(2024, 5, 1),
                end_date=date(2024, 10, 31),
                outcome="failed",
                reason_discontinued="Insufficient relief of constipation and bloating",
            ),
            PriorTherapy(
                drug="Amitiza (lubiprostone 8 mcg)",
                start_date=date(2024, 11, 1),
                end_date=date(2025, 1, 31),
                outcome="intolerant",
                reason_discontinued="Severe nausea, unable to continue",
            ),
        ],
        contraindications="Known hypersensitivity to linaclotide is absent",
        step_therapy_completed=True,
        date_of_last_treatment=date(2025, 1, 31),
        treatment_history_notes=(
            "Two adequate trials of preferred agents per payer step-therapy protocol completed."
        ),
    ),
    request=RequestMeta(
        request_type="new",
        is_expedited=False,
        previous_auth_number=None,
        date_of_request=date(2026, 6, 24),
        requested_duration="12 months",
        signature="Robert Testprescriber MD",
        signature_date=date(2026, 6, 24),
    ),
)


if __name__ == "__main__":
    import json
    print(json.dumps(GOLDEN_PATIENT.model_dump(mode="json", exclude_none=True), indent=2))
