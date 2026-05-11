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


class TemplateManifest(BaseModel):
    """
    All metadata for a stored template.  Persisted as manifest.json alongside
    the template.pdf in the template directory.
    """
    id: str
    name: str
    category: str = "prior_authorization"
    specialty: Optional[str] = None          # "gi_motility", "neurology", …
    drug: Optional[TemplateDrug] = None
    payer: Optional[TemplatePayer] = None
    indications: List[str] = Field(default_factory=list)  # clinical indications
    questions: List[TemplateQuestion] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    version: Optional[str] = None            # form version / revision string
    pages: Optional[int] = None
    is_public: bool = True
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
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
