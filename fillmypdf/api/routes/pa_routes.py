"""
PA Fill Routes
==============
POST /api/v1/pa/fill  — fill a PA form from a canonical patient JSON and return a PDF.
GET  /api/v1/pa/forms — list available blank forms.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ...models.pa_canonical import (
    PARequest, Patient, Insurance, Prescriber,
    Facility, Medication, Clinical, RequestMeta,
)
from ...services.pa_fill_service import fill_pa_form
from ...services.pa_map_store import PAMapStore

router = APIRouter(prefix="/pa", tags=["pa"])

# Blank forms shipped with the app
_FORMS_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "pa_forms"

# Offline map DB (built by pa_schema_extractor.py; optional — degrades gracefully)
_DB_PATH = Path(__file__).resolve().parent.parent.parent.parent / "pa_pipeline" / "schema_out" / "pa_forms.db"


def _get_store() -> Optional[PAMapStore]:
    if _DB_PATH.exists():
        return PAMapStore(_DB_PATH)
    return None


# ---------------------------------------------------------------------------
# List available forms
# ---------------------------------------------------------------------------

@router.get("/forms", summary="List available blank PA forms")
async def list_forms():
    forms = []
    if _FORMS_DIR.exists():
        for f in sorted(_FORMS_DIR.glob("*.pdf")):
            forms.append({
                "id": f.stem,
                "filename": f.name,
                "label": f.stem.replace("_", " ").title(),
                "blank_url": f"/static/pa_forms/{f.name}",
                "size_kb": round(f.stat().st_size / 1024, 1),
            })
    return {"forms": forms}


# ---------------------------------------------------------------------------
# Fill a form
# ---------------------------------------------------------------------------

class PAFillRequest(BaseModel):
    form_id: str = "texas_medicaid_pa"

    # Patient
    patient_full_name: Optional[str] = None
    patient_first_name: Optional[str] = None
    patient_last_name: Optional[str] = None
    patient_dob: Optional[str] = None          # YYYY-MM-DD or MM/DD/YYYY
    patient_sex: Optional[str] = None
    patient_address: Optional[str] = None
    patient_city: Optional[str] = None
    patient_state: Optional[str] = None
    patient_zip: Optional[str] = None
    patient_phone: Optional[str] = None
    patient_email: Optional[str] = None

    # Insurance
    payer_name: Optional[str] = None
    member_id: Optional[str] = None
    group_number: Optional[str] = None
    plan_name: Optional[str] = None

    # Prescriber
    prescriber_full_name: Optional[str] = None
    prescriber_npi: Optional[str] = None
    prescriber_fax: Optional[str] = None
    prescriber_phone: Optional[str] = None
    prescriber_specialty: Optional[str] = None
    prescriber_address: Optional[str] = None
    prescriber_city: Optional[str] = None
    prescriber_state: Optional[str] = None
    prescriber_zip: Optional[str] = None
    facility_name: Optional[str] = None
    facility_npi: Optional[str] = None
    facility_tax_id: Optional[str] = None

    # Medication
    drug_name: Optional[str] = None
    ndc: Optional[str] = None
    strength: Optional[str] = None
    sig: Optional[str] = None
    quantity: Optional[int] = None
    days_supply: Optional[int] = None
    diagnosis_code: Optional[str] = None
    clinical_rationale: Optional[str] = None
    request_type: Optional[str] = None         # new | renewal
    is_expedited: Optional[bool] = None
    date_of_request: Optional[str] = None       # YYYY-MM-DD


def _to_pa_request(body: PAFillRequest) -> PARequest:
    from datetime import date
    from fillmypdf.services.pa_normalize import normalize

    def _d(s: Optional[str]) -> Optional[date]:
        if not s:
            return None
        normed = normalize(s, "date")
        if not normed:
            return None
        try:
            m, d_, y = normed.split("/")
            return date(int(y), int(m), int(d_))
        except Exception:
            return None

    return PARequest(
        patient=Patient(
            full_name=body.patient_full_name,
            first_name=body.patient_first_name,
            last_name=body.patient_last_name,
            dob=_d(body.patient_dob),
            sex=body.patient_sex,
            address_line1=body.patient_address,
            city=body.patient_city,
            state=body.patient_state,
            zip=body.patient_zip,
            phone=body.patient_phone,
            email=body.patient_email,
        ),
        insurance=Insurance(
            payer_name=body.payer_name,
            member_id=body.member_id,
            group_number=body.group_number,
            plan_name=body.plan_name,
        ),
        prescriber=Prescriber(
            full_name=body.prescriber_full_name,
            npi=body.prescriber_npi,
            fax=body.prescriber_fax,
            phone=body.prescriber_phone,
            specialty=body.prescriber_specialty,
            address_line1=body.prescriber_address,
            city=body.prescriber_city,
            state=body.prescriber_state,
            zip=body.prescriber_zip,
        ),
        facility=Facility(
            name=body.facility_name,
            npi=body.facility_npi,
            tax_id=body.facility_tax_id,
        ),
        medication=Medication(
            drug_name=body.drug_name,
            ndc=body.ndc,
            strength=body.strength,
            sig=body.sig,
            quantity=body.quantity,
            days_supply=body.days_supply,
        ),
        clinical=Clinical(
            primary_diagnosis_code=body.diagnosis_code,
            clinical_rationale=body.clinical_rationale,
        ),
        request=RequestMeta(
            request_type=body.request_type,
            is_expedited=body.is_expedited,
            date_of_request=_d(body.date_of_request),
        ),
    )


@router.post("/fill", summary="Fill a PA form and download the filled PDF")
async def fill_pa(body: PAFillRequest):
    form_path = _FORMS_DIR / f"{body.form_id}.pdf"
    if not form_path.exists():
        available = [f.stem for f in _FORMS_DIR.glob("*.pdf")] if _FORMS_DIR.exists() else []
        raise HTTPException(
            404,
            f"Form '{body.form_id}' not found. Available: {available}"
        )

    pdf_bytes = form_path.read_bytes()
    request = _to_pa_request(body)
    store = _get_store()

    filled_bytes, report = fill_pa_form(
        pdf_bytes,
        request,
        pdf_filename=str(form_path),
        store=store,
        use_qwen_fallback=False,
    )

    s = report.summary()
    filename = f"{body.form_id}_filled.pdf"

    return StreamingResponse(
        io.BytesIO(filled_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Fill-Rate": str(s["fill_rate"]),
            "X-Fields-Filled": str(s["filled"]),
            "X-Fields-Total": str(s["total_fields"]),
        },
    )


@router.post("/fill/report", summary="Fill a PA form and return JSON provenance report")
async def fill_pa_report(body: PAFillRequest):
    form_path = _FORMS_DIR / f"{body.form_id}.pdf"
    if not form_path.exists():
        raise HTTPException(404, f"Form '{body.form_id}' not found.")

    pdf_bytes = form_path.read_bytes()
    request = _to_pa_request(body)
    store = _get_store()

    _, report = fill_pa_form(
        pdf_bytes, request,
        pdf_filename=str(form_path),
        store=store,
        use_qwen_fallback=False,
    )
    return {
        "summary": report.summary(),
        "fields": report.to_csv_rows(),
    }
