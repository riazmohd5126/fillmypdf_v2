"""
Form Template Library — Pydantic models
========================================
Describes a reusable PDF template (e.g. a specific PA form for a specific drug
and payer).  Separates metadata / question structure from the raw PDF bytes so
clients can browse the catalog and render their own UIs without downloading the
PDF first.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class TemplateDrug(BaseModel):
    """Drug this PA form is for."""
    name: str
    generic_name: Optional[str] = None
    strengths: List[str] = Field(default_factory=list)   # ["72mcg", "145mcg"]
    form: Optional[str] = None                           # "capsule", "tablet", "injection"
    j_code: Optional[str] = None                         # Medical-benefit J-code
    ndc: Optional[str] = None


class TemplatePayer(BaseModel):
    """Insurance / PBM that owns this PA form."""
    name: str
    plan_type: Optional[Literal["medicaid", "medicare", "commercial", "other"]] = None
    state: Optional[str] = None   # 2-letter abbreviation; None = national
    fax: Optional[str] = None
    phone: Optional[str] = None


class TemplateQuestion(BaseModel):
    """One question in the PA questionnaire."""
    key: str
    text: str
    type: Literal["yesno", "text", "checkbox", "date", "select"] = "yesno"
    required: bool = True
    options: List[str] = Field(default_factory=list)   # for 'select' / 'checkbox'
    section: Optional[str] = None                      # e.g. "initial", "continuation"
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Core manifest — written to manifest.json inside each template directory
# ---------------------------------------------------------------------------


class SignatureField(BaseModel):
    """
    A pre-defined signature zone stored in a template manifest.

    Coordinates use the same percentage convention as the signing endpoint
    (origin bottom-left, matching PDF convention):
      x_pct / y_pct  — left and bottom edges as % of page width/height
      width_pct / height_pct — box dimensions as % of page width/height
    """
    key: str = Field(..., min_length=1, max_length=80,
                     description="Machine-readable identifier, e.g. 'patient_signature'")
    label: str = Field(..., min_length=1, max_length=120,
                       description="Human-readable label shown in UI, e.g. 'Patient Signature'")
    page_index: int = Field(0, ge=0, description="Zero-based page number")
    x_pct: float = Field(55.0, ge=0, le=100, description="Left edge (% of page width)")
    y_pct: float = Field(5.0, ge=0, le=100, description="Bottom edge (% of page height)")
    width_pct: float = Field(40.0, ge=0.1, le=100, description="Box width (% of page width)")
    height_pct: float = Field(12.0, ge=0.1, le=100, description="Box height (% of page height)")
    required: bool = True
    description: Optional[str] = None


class TemplateManifest(BaseModel):
    """
    Domain-agnostic metadata for any stored PDF template.
    Persisted as manifest.json alongside template.pdf.

    Every form type (insurance, healthcare PA, tax, HR, general) uses the same
    core fields.  Domain-specific metadata goes in `custom` — no schema change
    needed when adding a new vertical.

    Healthcare PA example:
        category="prior_authorization", custom={"drug": "Linzess", "payer": "Molina"}
    Insurance example:
        category="commercial_insurance", custom={"acord_number": "125"}
    Tax example:
        category="tax", custom={"form_number": "1040", "tax_year": "2024"}
    """
    id: str
    name: str
    # Free-form — no enforced enum.  Common values: "prior_authorization",
    # "commercial_insurance", "tax", "employment", "real_estate", "general".
    category: str = "general"
    # Healthcare PA convenience fields — kept for backward-compat with existing
    # manifests.  New code should put domain data in `custom` instead.
    specialty: Optional[str] = None
    drug: Optional[TemplateDrug] = None
    payer: Optional[TemplatePayer] = None
    indications: List[str] = Field(default_factory=list)
    questions: List[TemplateQuestion] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    version: Optional[str] = None
    pages: Optional[int] = None
    is_public: bool = True
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Pre-defined signature zones for this template.
    # Consumers can call POST /templates/{id}/sign with a field key
    # instead of supplying raw coordinates.
    signature_fields: List[SignatureField] = Field(default_factory=list)
    # Catch-all for any domain-specific metadata.
    # Insurance:  {"acord_number": "125", "line": "commercial_property", "state": "TX"}
    # Healthcare: {"npi": "1234567890", "specialty": "GI"}
    # Tax:        {"form_number": "1040", "tax_year": "2024"}
    custom: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# API response shapes
# ---------------------------------------------------------------------------


class TemplateListItem(BaseModel):
    """Lightweight row returned by GET /templates."""
    id: str
    name: str
    category: str
    drug_name: Optional[str] = None
    payer_name: Optional[str] = None
    plan_type: Optional[str] = None
    state: Optional[str] = None
    specialty: Optional[str] = None
    indications: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    pages: Optional[int] = None
    question_count: int = 0
    is_public: bool = True


class TemplateListResponse(BaseModel):
    templates: List[TemplateListItem]
    total: int


class TemplateFillResponse(BaseModel):
    success: bool
    template_id: str
    fields_detected: int
    fields_filled: int
    fields_skipped_low_confidence: int = 0
    avg_confidence: Optional[float] = None
    cache_hit: bool = False
    download_url: str
    message: Optional[str] = None
    # Optional verbose fields — only populated when return_mappings=true
    mappings: Optional[Dict[str, str]] = None
    confidence: Optional[Dict[str, float]] = None
    field_labels: Optional[Dict[str, str]] = None


class TemplateBatchResponse(BaseModel):
    success: bool
    template_id: str
    batch_id: str
    total_records: int
    successful: int
    failed: int
    success_rate: float
    cache_hits: int = 0
    avg_confidence: Optional[float] = None
    download_url: str
    message: Optional[str] = None


class SignatureFieldsResponse(BaseModel):
    template_id: str
    signature_fields: List[SignatureField]
    total: int


class TemplateSignResponse(BaseModel):
    """Returned by POST /templates/{id}/sign."""
    success: bool = True
    template_id: str
    field_key: str
    filename: str
    download_url: str
    certificate_url: str
    document_hash: str
    audit_id: str
    message: str = "Signature overlay applied."
