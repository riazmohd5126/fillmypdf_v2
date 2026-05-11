"""Models for Smart Data Extraction (AcroForm → structured data)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ExtractFieldItem(BaseModel):
    name: str = Field(description="AcroForm field name")
    label: Optional[str] = Field(
        None,
        description="Inferred printable label near the field (when include_labels=true)",
    )
    value: str = Field(default="", description="Current field value (/V)")
    page: Optional[int] = Field(None, ge=1, description="1-based page (when known)")
    field_type: Optional[str] = Field(
        None, description="text, checkbox, or other (when include_labels=true)"
    )


class PdfExtractResponse(BaseModel):
    """Response for POST /api/v1/extract."""

    success: bool
    fields_detected: int
    non_empty_fields: int
    filename: Optional[str] = None
    fields: List[ExtractFieldItem] = Field(default_factory=list)
    message: Optional[str] = Field(
        None,
        description="Hints when no widgets found (flat PDF needs fillable conversion first)",
    )
