"""
Card Capture models
====================
Two insurance-card photos (front + back) → the 7 canonical insurance fields,
each with a confidence score and a normalized bounding box so the UI can
show the exact pixel crop next to the extracted value (catches things a
format check can't, like a misread digit in a member ID).
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# The 7 fields the spec calls for, mapped to this app's existing canonical
# insurance.* paths (see fillmypdf/models/pa_canonical.py) so extracted
# values drop straight into the same profile/fill pipeline as everything else.
CARD_FIELDS = (
    "payer_name",
    "member_name",
    "member_id",
    "group_number",
    "rx_bin",
    "rx_pcn",
    "rx_group",
)

# canonical.* path each field maps to, for handing values to ProfileService /
# PA fill downstream.
CANONICAL_PATH = {
    "payer_name": "insurance.payer_name",
    "member_name": "insurance.subscriber_name",
    "member_id": "insurance.member_id",
    "group_number": "insurance.group_number",
    "rx_bin": "insurance.rx_bin",
    "rx_pcn": "insurance.rx_pcn",
    "rx_group": "insurance.rx_group",
}


class BBox(BaseModel):
    """Normalized (0-1) bounding box relative to the submitted image."""

    x0: float
    y0: float
    x1: float
    y1: float


class CardField(BaseModel):
    field: str
    value: Optional[str] = None
    confidence: float = 0.0
    side: Optional[Literal["front", "back"]] = None
    bbox: Optional[BBox] = None

    # Deterministic validation (no model involved).
    valid: bool = True
    validation_reason: Optional[str] = None
    known_bin_match: Optional[str] = None  # payer name if rx_bin matched a known list


class MatchedTemplate(BaseModel):
    template_id: str
    name: str
    payer_name: Optional[str] = None
    score: float


class CardCaptureResponse(BaseModel):
    fields: List[CardField] = Field(default_factory=list)
    matched_templates: List[MatchedTemplate] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    engine: str = Field(
        default="auto",
        description="Requested engine: auto | tesseract | vision.",
    )
    engine_used: str = Field(
        default="vision",
        description="Engine that produced the fields: tesseract | vision | hybrid.",
    )

    def as_canonical(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for f in self.fields:
            if f.value and f.field in CANONICAL_PATH:
                out[CANONICAL_PATH[f.field]] = f.value
        return out
