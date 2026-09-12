"""
Note Paste models
==================
A nurse pastes visit notes; the model drafts a clinical-justification answer
with quoted evidence. See ``services/note_paste_service.py`` for the
deterministic verification layer (every quote must appear verbatim in the
text that was actually sent to the model, and durations are computed in
Python, never trusted from the model).
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class EvidenceQuote(BaseModel):
    quote: str
    date: Optional[str] = None
    verified: bool = False


class NotePasteExtractRequest(BaseModel):
    notes_text: str = Field(..., min_length=1, max_length=20000)
    question: Optional[str] = Field(
        default=None,
        description="The step-therapy / clinical question to answer, e.g. "
        "'Has the patient tried and failed a conventional DMARD for at "
        "least 3 months at an optimized dose?'. Defaults to a generic "
        "conventional-DMARD step-therapy question for indication=rheumatoid_arthritis.",
    )
    indication: str = Field(
        default="rheumatoid_arthritis",
        description="Selects the knowledge file under fillmypdf/knowledge/. "
        "Only 'rheumatoid_arthritis' ships today.",
    )
    ai_provider: Optional[Literal["gemini", "local"]] = None
    ai_api_key: Optional[str] = None
    ai_base_url: Optional[str] = None
    ai_model: Optional[str] = None


class NotePasteExtractResponse(BaseModel):
    answer: Literal["yes", "no", "not_in_notes"]
    confidence: Literal["high", "medium", "review_required"]
    drug: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    optimized_dose_start: Optional[str] = None
    summary: str = ""
    evidence: List[EvidenceQuote] = Field(default_factory=list)

    # Computed server-side — never trust model arithmetic.
    total_months: Optional[float] = None
    optimized_months: Optional[float] = None

    # Deterministic verification outcome.
    all_quotes_verified: bool = True
    review_flags: List[str] = Field(default_factory=list)

    # The de-identified text actually sent to the model (so the UI can
    # highlight quotes against the exact string quotes were checked against).
    sent_text: str = ""
