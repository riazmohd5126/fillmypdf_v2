"""
form_spec.py
============
The **per-form** half of a form's intake definition — everything deliberately
NOT generalized into the canonical catalog.

A form's intake description is stored as two independent halves:

``canonical map``  text/identity fields → fixed catalog paths. Small, reusable
                   across forms, prefillable from a stored patient profile.
``form spec``      checkbox questions, free-text narratives and signatures,
                   kept verbatim in the wording the form itself uses.

Checkbox questions vary too much between payers to force into a fixed schema
("Which Care Category is this appeal related to?" is meaningful only on the form
that asks it), so they are captured as-authored instead. Splitting them also
keeps the two halves versioning independently: re-running label extraction can
improve a form spec without invalidating a canonical map a human already locked.

PHI-free: this describes the blank form only.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class RuleClause(BaseModel):
    """One atomic condition: controlling question id holds ``equals`` (or any value)."""

    field: str = Field(description="Form-spec question id (Guided Fill ``q:<id>``)")
    equals: Optional[str] = Field(
        None,
        description="Required answer label. None means any non-empty selection.",
    )


class VisibilityRule(BaseModel):
    """Executable show/enable condition for a field or question group.

    Simple form: ``field`` + ``equals`` (one controlling question).
    Compound form: ``all`` (AND) and/or ``any`` (OR of nested rules) for
    lettered skip cascades (A→B→C→D).
    """

    field: Optional[str] = Field(
        None, description="Controlling question id (simple single-condition rules)"
    )
    equals: Optional[str] = Field(
        None,
        description=(
            "Value the controlling widget must hold. None means 'is checked' "
            "(any non-Off state)."
        ),
    )
    all: Optional[List[RuleClause]] = Field(
        None, description="AND — every clause must match"
    )
    any: Optional[List["VisibilityRule"]] = Field(
        None, description="OR — at least one nested rule must match"
    )
    source: Literal["linked_field", "conditional_text", "skip_logic"] = Field(
        description=(
            "'linked_field' — same-row geometry (How Long next to Yes). "
            "'conditional_text' — parsed from AI prose. "
            "'skip_logic' — derived from printed go-to / skip item letters."
        )
    )
    raw: Optional[str] = Field(
        None, description="Original prose the rule was derived from, for audit"
    )


class QuestionOption(BaseModel):
    """One selectable box within a question group."""

    field: str = Field(description="Map key (``name`` or ``name::export``)")
    acro_field: str = Field(description="AcroForm /T name to tick when chosen")
    export: Optional[str] = Field(
        None, description="Appearance state that marks this option as on"
    )
    label: str = Field(description="Printed option text, verbatim")
    order: int = Field(description="Position within the group, in PDF order")
    skip_logic: Optional[str] = Field(
        None,
        description=(
            "Printed branch for THIS option (e.g. Yes→'go to item B', "
            "No→'skip items B & C'). Differs per option on the same question."
        ),
    )


class QuestionGroup(BaseModel):
    """A checkbox/radio question, kept in the form's own wording."""

    id: str = Field(description="Stable slug derived from the question text")
    question: str = Field(description="Group question, verbatim from the form")
    input: Literal["radio", "checkbox"] = Field(
        description=(
            "'radio' when the PDF enforces single-select (one AcroForm field, "
            "several exports); 'checkbox' when boxes are independent fields."
        )
    )
    options: List[QuestionOption] = Field(default_factory=list)
    section: Optional[str] = None
    subsection: Optional[str] = None
    page: Optional[int] = Field(None, description="1-based page")
    order: int = Field(0, description="Position on the form, in reading order")
    conditional: Optional[str] = Field(
        None, description="Prose condition hint, verbatim"
    )
    skip_logic: Optional[str] = Field(
        None, description="Printed branch/skip instruction, verbatim"
    )
    rule: Optional[VisibilityRule] = None
    canonical_hint: Optional[str] = Field(
        None,
        description=(
            "OPTIONAL catalog path this question also answers, so genuinely "
            "recurring questions (expedited/urgent, new vs continuation) can "
            "still prefill from a patient profile. Never required, and never "
            "set automatically — a reviewer opts in."
        ),
    )


class LongTextField(BaseModel):
    """A multiline narrative box (clinical rationale, comments)."""

    field: str
    acro_field: str
    label: str
    section: Optional[str] = None
    subsection: Optional[str] = None
    page: Optional[int] = None
    order: int = 0
    conditional: Optional[str] = None
    skip_logic: Optional[str] = None
    rule: Optional[VisibilityRule] = None


class SignatureField(BaseModel):
    """A signature (or signature-date) widget kept out of the canonical map.

    Includes true PDF ``/Sig`` fields and CareFirst-style ``/Tx`` blanks whose
    caption is a printed signature line. Companion date blanks next to the
    line use ``kind='date'``. Guided Fill collects typed values as ``t:<field>``.
    """

    field: str
    acro_field: str
    label: str
    section: Optional[str] = None
    page: Optional[int] = None
    order: int = 0
    kind: Literal["signature", "date"] = "signature"
    role: Optional[str] = Field(
        None, description="Signer role (prescriber, patient, …) — reviewer-assigned"
    )


class TableColumn(BaseModel):
    """One column of a form-specific repeating table."""

    id: str = Field(description="Stable slug from the printed header")
    header: str = Field(
        description="Printed column header from extract (``column`` / label)"
    )
    fields: List[str] = Field(
        default_factory=list,
        description="AcroForm /T names top→bottom (one per table row)",
    )


class FormTable(BaseModel):
    """A multi-row table kept in the form's own wording (not the catalog).

    Typical PA history grids: Drug Name | Dates of Therapy | Reason for
    Discontinuation. Headers and cells stay form-specific; Guided Fill renders
    a grid and unlocks it via ``rule`` when skip logic reaches this section.
    """

    id: str
    title: Optional[str] = Field(
        None, description="Printed table / subsection title, verbatim"
    )
    section: Optional[str] = None
    subsection: Optional[str] = None
    page: Optional[int] = None
    order: int = 0
    columns: List[TableColumn] = Field(default_factory=list)
    row_count: int = 0
    rule: Optional[VisibilityRule] = None


class ExtraField(BaseModel):
    """A leftover widget with no real catalog path — form-specific Guided Fill.

    Unmapped / ``other`` data boxes and orphan checkboxes that are not already
    owned by questions, tables, narratives or signatures. Values submit as
    ``t:<field>`` and write straight to the AcroForm name.
    """

    field: str = Field(description="Map key (AcroForm /T, or name::export for radios)")
    acro_field: str = Field(description="AcroForm /T name to write")
    label: str
    kind: Literal["text", "checkbox", "longtext"] = "text"
    section: Optional[str] = None
    subsection: Optional[str] = None
    page: Optional[int] = None
    order: int = 0
    export: Optional[str] = Field(
        None, description="Checkbox/radio on-state when kind is checkbox"
    )


class FormSpec(BaseModel):
    """Per-form questions, tables, narratives and signatures for one blank form."""

    signature: str = Field(description="Form structure signature (shared with the canonical map)")
    form_label: Optional[str] = None
    built_at: Optional[str] = None
    reviewed: bool = False
    questions: List[QuestionGroup] = Field(default_factory=list)
    tables: List[FormTable] = Field(default_factory=list)
    long_text: List[LongTextField] = Field(default_factory=list)
    signatures: List[SignatureField] = Field(default_factory=list)
    extras: List[ExtraField] = Field(default_factory=list)
    # Bumped when Guided Fill leftover extras were introduced; schema refresh
    # rebuilds specs still at 0 so cached forms pick up Additional fields.
    extras_version: int = 0
    # Bumped when typed /Tx signature lines (+ companion dates) were added to
    # FormSpec.signatures. Schema refresh full-rebuilds specs still at 0.
    signatures_version: int = 0

    @property
    def option_count(self) -> int:
        return sum(len(q.options) for q in self.questions)

    @property
    def table_field_keys(self) -> set:
        """All AcroForm names owned by form-specific tables."""
        keys: set = set()
        for t in self.tables:
            for c in t.columns:
                keys.update(c.fields)
        return keys

    @property
    def extra_field_keys(self) -> set:
        """Map keys / AcroForm names owned by leftover extras."""
        keys: set = set()
        for e in self.extras or []:
            if e.field:
                keys.add(e.field)
            if e.acro_field:
                keys.add(e.acro_field)
        return keys
