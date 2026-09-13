"""
Note Paste models
==================
A nurse pastes visit notes; the model drafts a clinical-justification answer
with quoted evidence. See ``services/note_paste_service.py`` for the
deterministic verification layer (every quote must appear verbatim in the
text that was actually sent to the model, and durations are computed in
Python, never trusted from the model).

The response is a list of drug **trials**, not a single drug/dates/evidence
set — a patient may have tried and failed more than one conventional DMARD,
and the knowledge file explicitly asks the model to report every one it
finds. Each trial carries its own evidence, independently verified, so a
summary that mentions two drugs but only backs one of them with a real
quote gets caught per-drug, not just "was there any evidence at all."
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class EvidenceQuote(BaseModel):
    quote: str
    date: Optional[str] = None
    verified: bool = False


class DrugTrial(BaseModel):
    drug: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    optimized_dose_start: Optional[str] = None
    evidence: List[EvidenceQuote] = Field(default_factory=list)

    # Computed server-side for this trial — never trust model arithmetic.
    total_months: Optional[float] = None
    optimized_months: Optional[float] = None

    # True only if every evidence quote for THIS trial verified verbatim.
    # A trial with zero evidence entries is also unverified — a drug named
    # in the summary with nothing backing it is exactly the gap this exists
    # to catch.
    all_quotes_verified: bool = False


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
    summary: str = ""
    trials: List[DrugTrial] = Field(default_factory=list)

    # True only if every trial's every quote verified. False the moment any
    # single trial (any one of possibly several drugs) has an unverified or
    # missing quote — not just "was at least one thing verified somewhere."
    all_quotes_verified: bool = True
    review_flags: List[str] = Field(default_factory=list)

    # The de-identified text actually sent to the model (so the UI can
    # highlight quotes against the exact string quotes were checked against).
    sent_text: str = ""
