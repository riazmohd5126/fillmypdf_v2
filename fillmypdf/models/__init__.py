"""
FillMyPDF Models
================
Pydantic models for data validation
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Optional, Dict, Any, Literal, List
from datetime import datetime
import re


# ============================================================================
# Auth / API Key Models
# ============================================================================

Tier = Literal["free", "pro", "business", "admin"]


class APIKeyCreate(BaseModel):
    """Request body for creating a new API key"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "CI — production fills",
                "tier": "pro",
                "owner": "integrations@company.example",
            }
        }
    )

    name: str = Field(..., min_length=1, max_length=100,
                      description="Human-friendly name (e.g. 'Production server')")
    tier: Tier = Field(default="free",
                       description="Subscription tier; gates rate limits and profile counts")
    owner: Optional[str] = Field(default=None, max_length=200,
                                 description="Optional contact email or owner identifier")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Name cannot be empty")
        return v.strip()


class APIKey(BaseModel):
    """Public-safe view of an API key (never includes the plaintext key)"""
    id: str
    name: str
    tier: Tier
    owner: Optional[str] = None
    prefix: str                                  # first 12 chars of the key, for display
    created_at: datetime
    last_used_at: Optional[datetime] = None
    request_count: int = 0
    revoked: bool = False


class APIKeyCreateResponse(APIKey):
    """One-time response that includes the plaintext key (shown only at creation)"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "key_demo01",
                "name": "CI — production fills",
                "tier": "pro",
                "owner": "integrations@company.example",
                "prefix": "fmp_demo01",
                "created_at": "2026-05-09T14:22:33",
                "last_used_at": None,
                "request_count": 0,
                "revoked": False,
                "key": "fmp_live_REDACTED_SAVE_ONCE",
            }
        }
    )

    key: str = Field(..., description="The plaintext API key. SAVE IT NOW — it will never be shown again.")


# ============================================================================
# Profile Models
# ============================================================================

class ProfileCreate(BaseModel):
    """Create new profile"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Metro Cardiology Clinic",
                "profile_type": "business",
                "data": {"npi": "1234567890", "tax_id_last4": "9012", "phone": "(555) 010-9876"},
            }
        }
    )

    name: str = Field(..., min_length=1, max_length=100)
    profile_type: Literal["personal", "business", "spouse", "dependent", "custom"] = "personal"
    data: Dict[str, str] = Field(default_factory=dict)
    
    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        if not v.strip():
            raise ValueError("Name cannot be empty")
        return v.strip()


class ProfileUpdate(BaseModel):
    """Update existing profile"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    profile_type: Optional[Literal["personal", "business", "spouse", "dependent", "custom"]] = None
    data: Optional[Dict[str, str]] = None


class Profile(BaseModel):
    """Profile response model"""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "prof_demo01",
                "name": "Metro Cardiology Clinic",
                "profile_type": "business",
                "created_at": "2026-05-01T10:00:00",
                "updated_at": "2026-05-09T11:45:22",
                "usage_count": 14,
                "data_preview": {"npi": "1234567890", "tax_id_last4": "9012"},
            }
        },
    )

    id: str
    name: str
    profile_type: str
    created_at: datetime
    updated_at: datetime
    usage_count: int = 0
    data_preview: Dict[str, str] = Field(default_factory=dict)


# ============================================================================
# API Response Models
# ============================================================================

class HealthResponse(BaseModel):
    """Health check response"""

    model_config = ConfigDict(
        json_schema_extra={"example": {"status": "healthy", "version": "4.0.0"}}
    )

    status: str
    version: str


class UsageStats(BaseModel):
    """Usage statistics"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_requests": 12840,
                "requests_today": 312,
                "profiles_created": 42,
                "last_reset": "2026-05-09T00:05:01",
            }
        }
    )

    total_requests: int
    requests_today: int
    profiles_created: int
    last_reset: datetime


class ErrorResponse(BaseModel):
    """Error response"""
    error: str
    detail: Optional[str] = None


# ============================================================================
# Batch Processing Models
# ============================================================================

class BatchResult(BaseModel):
    """Single record batch result"""
    index: int
    filename: Optional[str] = None
    success: bool
    fields_filled: Optional[int] = None
    error: Optional[str] = None


class BatchResponse(BaseModel):
    """Batch processing response"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "batch_id": "batch_a1b2c3",
                "total_records": 120,
                "successful": 118,
                "failed": 2,
                "success_rate": 98.333,
                "download_url": "/api/v1/batch/download/batch_a1b2c3_results.zip",
                "profile_used": "prof_demo01",
                "message": "Batch completed with 118 successful fills",
            }
        }
    )

    success: bool
    batch_id: str
    total_records: int
    successful: int
    failed: int
    success_rate: float
    download_url: str
    profile_used: Optional[str] = None
    message: str


# ============================================================================
# PDF Processing Models
# ============================================================================

class PDFConversionResult(BaseModel):
    """PDF conversion result"""
    success: bool
    fields_detected: int
    output_path: str
    message: Optional[str] = None


class AutofillResult(BaseModel):
    """Autofill result"""
    success: bool
    fields_detected: int
    fields_filled: int
    fields_verified: int
    pages_analyzed: int
    output_path: str
    field_names: list = Field(default_factory=list)
    mappings: Dict[str, str] = Field(default_factory=dict)
    unmapped_fields: list = Field(default_factory=list)


class FormFieldInspectionItem(BaseModel):
    """One AcroForm field after conversion + inferred on-page label."""

    name: str
    field_type: Literal["text", "checkbox", "other"]
    page: int
    label: str
    x0: int = 0
    x1: int = 0
    y: int = 0


class FormTemplateInspectionResponse(BaseModel):
    """Layer 3: introspect a template PDF before burning AI fills."""

    success: bool
    fields_detected: int
    fields: List[FormFieldInspectionItem] = Field(default_factory=list)
    message: Optional[str] = None


class SignatureApplyResponse(BaseModel):
    """Visual signature overlay applied (PNG stamp — not cryptographic PAdES)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "filename": "signed_a1b2c3d4.pdf",
                "download_url": "/api/v1/batch/download/signed_a1b2c3d4.pdf",
                "page_index": 0,
                "message": "Signature overlay applied.",
            }
        }
    )

    success: bool = True
    filename: str
    download_url: str
    page_index: int
    message: str = "Signature overlay applied."
