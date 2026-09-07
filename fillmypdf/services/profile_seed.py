"""Install shared demo profiles any authenticated clinic can read.

Profiles live under ``storage/profiles/`` (gitignored, and hidden by Render's
disk mount). Creating them at boot through the repository encrypts sensitive
fields with *this* instance's ``PROFILES_ENCRYPTION_KEY``.

Existing ids are left untouched so a redeploy never duplicates or overwrites
clinic data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

SEED_OWNER = "seed_demo"
SEED_ORG = "org_demo"

# Stable ids so skip-if-exists is deterministic across deploys.
DEMO_PROFILES: List[Dict] = [
    {
        "id": "prof_demo_patient",
        "name": "Demo — Jordan Patient",
        "profile_type": "patient",
        "data": {
            "patient_first_name": "Jordan",
            "patient_last_name": "Demo",
            "patient_full_name": "Jordan Demo",
            "patient_dob": "1988-04-12",
            "patient_gender": "Female",
            "member_id": "DEMO-MBR-0001",
            "group_number": "DEMO-GRP-99",
            "insurance_plan": "Demo Health Plan",
            "payer_name": "Demo Health Plan",
            "subscriber_name": "Jordan Demo",
            "patient_address": "100 Trial Street",
            "patient_city": "Austin",
            "patient_state": "TX",
            "patient_zip": "78701",
            "patient_phone": "(555) 010-1001",
            "patient_email": "jordan.demo@example.com",
        },
    },
    {
        "id": "prof_demo_provider",
        "name": "Demo — Dr. Alex Prescriber",
        "profile_type": "provider",
        "data": {
            "provider_first_name": "Alex",
            "provider_last_name": "Demo",
            "provider_full_name": "Alex Demo, MD",
            "npi": "1999999992",
            "specialty": "Family Medicine",
            "practice_name": "Demo Family Medicine",
            "provider_address": "200 Trial Avenue",
            "provider_city": "Austin",
            "provider_state": "TX",
            "provider_zip": "78701",
            "provider_phone": "(555) 010-2002",
            "provider_fax": "(555) 010-2003",
            "dea_number": "AD1234563",
        },
    },
    {
        "id": "prof_demo_facility",
        "name": "Demo — Family Medicine Clinic",
        "profile_type": "facility",
        "data": {
            "facility_name": "Demo Family Medicine",
            "facility_npi": "1999999984",
            "facility_phone": "(555) 010-3003",
            "facility_fax": "(555) 010-3004",
            "facility_address": "200 Trial Avenue",
            "facility_city": "Austin",
            "facility_state": "TX",
            "facility_zip": "78701",
        },
    },
    {
        "id": "prof_demo_pharmacy",
        "name": "Demo — Specialty Pharmacy",
        "profile_type": "pharmacy",
        "data": {
            "pharmacy_name": "Demo Specialty Pharmacy",
            "pharmacy_npi": "1999999976",
            "pharmacy_store_number": "DEMO-01",
            "pharmacy_address": "300 Trial Boulevard",
            "pharmacy_city": "Austin",
            "pharmacy_state": "TX",
            "pharmacy_zip": "78702",
            "pharmacy_phone": "(555) 010-4004",
            "pharmacy_fax": "(555) 010-4005",
        },
    },
    {
        "id": "prof_demo_encounter",
        "name": "Demo — Sample PA encounter",
        "profile_type": "encounter",
        "data": {
            "drug_name": "Demozumab",
            "strength": "150 mg",
            "sig": "Inject 150 mg subcutaneously once monthly",
            "quantity": "1",
            "days_supply": "30",
            "dosage_form": "injection",
            "diagnosis_code": "L40.0",
            "diagnosis_description": "Psoriasis vulgaris (demo)",
            "clinical_rationale": "Demonstration encounter for trial fills. Not a real patient.",
            "request_type": "Prior Authorization",
            "requested_duration": "12 months",
        },
    },
]


def install_demo_profiles() -> Dict[str, int]:
    """Create missing shared demo profiles. Never overwrites existing files."""
    from ..repositories.profile_repository import ProfileRepository

    repo = ProfileRepository()
    now = datetime.now(timezone.utc).isoformat()
    created = 0
    skipped = 0
    for spec in DEMO_PROFILES:
        pid = spec["id"]
        if repo.get(pid) is not None:
            skipped += 1
            continue
        row = {
            "id": pid,
            "name": spec["name"],
            "profile_type": spec["profile_type"],
            "owner_id": SEED_OWNER,
            "org_id": SEED_ORG,
            "shared": True,
            "data": repo._encrypt_data(dict(spec["data"])),
            "created_at": now,
            "updated_at": now,
            "usage_count": 0,
        }
        if repo.save(row):
            created += 1
        else:
            skipped += 1
    return {"created": created, "skipped": skipped}
