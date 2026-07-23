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
    label_source: Optional[str] = Field(
        None,
        description=(
            "How the label was derived: 'geometry' (side scan), "
            "'table-header' (column header above, inside a real bordered "
            "grid of rows AND columns), 'cell-group' (label found above the "
            "field, but not inside a fully-bordered row/column grid), "
            "'export' (a Yes/No-style radio option's own AcroForm export "
            "value, e.g. 'Yes'/'No', used when the printed text near the "
            "widget is unreliable but the parent field name already reads "
            "as the real question), "
            "'name-match' (the field's own AcroForm name matched printed "
            "text found nearby, used when the field's rect is a row off "
            "from its own printed prompt and the normal scan grabbed "
            "unrelated text instead), "
            "'vision' (Gemini vision fallback), "
            "or 'name' (fell back to raw field name)"
        ),
    )
    section: Optional[str] = Field(
        None,
        description=(
            "Nearest section header above this field (e.g. 'Section III — Patient Information'). "
            "Helps disambiguate repeated labels like 'Name' across different form sections."
        ),
    )
    subsection: Optional[str] = Field(
        None,
        description=(
            "A sub-heading or instructional qualifier printed within the "
            "section that scopes this field — e.g. a sub-block title "
            "('Compound Drug Information') or a printed instruction "
            "('If this is a compound drug, complete this part'). None when the "
            "field is not under any sub-heading/instruction."
        ),
    )
    group: Optional[str] = Field(
        None,
        description=(
            "Checkbox group qualifier/header (e.g. 'Type of Transplant' or "
            "'Is the member diagnosed with Autism Spectrum Disorder'). "
            "Set only for checkbox-type fields whose option (e.g. 'Lung', 'Yes') "
            "needs a group question/header for context."
        ),
    )
    table: Optional[str] = Field(
        None,
        description=(
            "Shared identity for every field inside the same detected table "
            "(a real bordered grid of rows AND columns, label_source="
            "'table-header'): the table's own section header when it has "
            "one, otherwise a synthetic 'table_1', 'table_2', ... Lets "
            "consumers group a table's rows/columns together for autofill. "
            "None for fields outside any detected table."
        ),
    )
    column: Optional[str] = Field(
        None,
        description=(
            "For a cell inside a detected table, the column header text this "
            "field sits under (e.g. 'Drug Name', 'Strength', 'NDC #'). None "
            "outside a table or when no column header applies."
        ),
    )
    option: Optional[str] = Field(
        None,
        description=(
            "For a radio/checkbox GROUP (one AcroForm field with several "
            "option widgets, e.g. Gender = Male/Female), the export value of "
            "THIS option's row. Each option is emitted as its own row so all "
            "choices are visible; `value` echoes this option's export only "
            "when it is the one currently selected. None for plain fields."
        ),
    )
    value: str = Field(default="", description="Current field value (/V)")
    page: Optional[int] = Field(None, ge=1, description="1-based page (when known)")
    field_type: Optional[str] = Field(
        None, description="text, checkbox, signature, or other (when include_labels=true)"
    )
    linked_field: Optional[str] = Field(
        None,
        description=(
            "For an inline fill-in blank that belongs to a checkbox option "
            "(e.g. the date box in '☐ Continuation of therapy (date initiated: "
            "___)'), the AcroForm name of the checkbox this text field is "
            "attached to — detected by same-row adjacency. Lets consumers treat "
            "the pair as one conditional input. None when no such link is found."
        ),
    )
    conditional: Optional[str] = Field(
        None,
        description=(
            "Human-readable conditional-logic hint from the AI when a field "
            "only applies if another option is selected (e.g. \"Only if 'Other' "
            "is checked\"). Complements the deterministic `linked_field`. None "
            "when no condition was detected."
        ),
    )
    skip_logic: Optional[str] = Field(
        None,
        description=(
            "Printed branch/skip/stop instruction attached to this field or "
            "option, copied verbatim (e.g. \"No, skip to #9\", \"If Yes, no "
            "further questions\", \"If \u2264 -2.5, stop\"). Lets consumers "
            "honor form branching. None when the field has no such instruction."
        ),
    )
    confidence: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence (0-1) that this field's label is correct, derived from "
            "cross-source agreement (AcroForm /TU vs geometry vs AI) plus "
            "quality signals (missing section, truncated caption, duplicate "
            "label). Low values (< ~0.6) are good candidates for human review."
        ),
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
