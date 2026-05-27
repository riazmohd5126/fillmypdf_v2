"""
Smart Data Extraction
=======================
Read AcroForm field values from a PDF into structured records.

Designed for filled **fillable** PDFs (AcroForm). Static / scanned-only PDFs
return zero fields unless they already have form widgets — use fill pipeline
first to create widgets, then extract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from ..models.extract import ExtractFieldItem, PdfExtractResponse
from .pdf_service import PDFService
from .vision_service import VisionService


class ExtractionService:
    def __init__(self) -> None:
        self._pdf = PDFService()

    def extract_pdf(self, pdf_path: Path, *, include_labels: bool) -> PdfExtractResponse:
        """
        Read form field names and `/V` values. Optionally augment with
        pdfplumber-inferred labels (same path as `/template-fields` — no AI).
        """
        try:
            raw_values = self._pdf.get_form_fields(pdf_path)
        except Exception as exc:
            return PdfExtractResponse(
                success=False,
                fields_detected=0,
                non_empty_fields=0,
                message=f"Could not read PDF: {exc}",
            )

        label_map: Dict[str, str] = {}
        page_map: Dict[str, int] = {}
        type_map: Dict[str, str] = {}
        detected_names: List[str] = []

        # Build a leaf→full-path index so pdfplumber labels (leaf names) can be
        # matched against raw_values keys that may be fully-qualified dotted paths
        # e.g. "F[0].P1[0].Producer_FullName_A[0]" → leaf "Producer_FullName_A[0]"
        def _leaf(name: str) -> str:
            return name.split(".")[-1] if "." in name else name

        leaf_to_full: Dict[str, str] = {_leaf(k): k for k in raw_values}

        if include_labels:
            vision = VisionService(
                api_key="-", base_url="https://example.invalid/", model="none"
            )
            insp = vision.inspect_fillable_form(str(pdf_path))
            for row in insp.get("fields") or []:
                name = row.get("name")
                if not name:
                    continue
                # Resolve to full path if this is a leaf name from a nested form
                full_name = leaf_to_full.get(name, name)
                detected_names.append(full_name)
                label_map[full_name] = row.get("label") or name
                page_map[full_name] = int(row.get("page", 1) or 1)
                type_map[full_name] = row.get("field_type") or ""

        ordered_names: List[str] = []
        seen = set()
        for n in detected_names:
            if n not in seen:
                ordered_names.append(n)
                seen.add(n)
        for n in sorted(raw_values.keys()):
            if n not in seen:
                ordered_names.append(n)
                seen.add(n)

        fields: List[ExtractFieldItem] = []
        for name in ordered_names:
            val = raw_values.get(name, "")
            if not isinstance(val, str):
                val = str(val) if val is not None else ""
            lbl = label_map.get(name)
            ft = type_map.get(name) or None
            pg = page_map.get(name)
            fields.append(
                ExtractFieldItem(
                    name=name,
                    label=lbl if include_labels else None,
                    value=val,
                    page=pg,
                    field_type=ft,
                )
            )

        nonempty = sum(1 for f in fields if str(f.value).strip())
        hint = None
        if not fields:
            hint = (
                "No AcroForm fields detected. Upload a PDF that already contains "
                "form widgets (typically after conversion to fillable, or vendor fillable templates). "
                "Hand-filled static/scanned PDFs require OCR separately."
            )
        elif nonempty == 0 and fields:
            hint = (
                "Fields exist but appear empty (/V absent). Confirm the PDF was saved "
                "with embedded form values (some viewers flatten on save)."
            )

        return PdfExtractResponse(
            success=True,
            fields_detected=len(fields),
            non_empty_fields=nonempty,
            fields=fields,
            message=hint,
        )
