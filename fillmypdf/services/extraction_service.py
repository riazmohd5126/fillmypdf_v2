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
from typing import Dict, List, Optional

from ..models.extract import ExtractFieldItem, PdfExtractResponse
from .pdf_service import PDFService
from .vision_service import VisionService


class ExtractionService:
    def __init__(self) -> None:
        self._pdf = PDFService()

    def extract_pdf(
        self,
        pdf_path: Path,
        *,
        include_labels: bool,
        ai_labels: bool = False,
        ai_api_key: str = "",
        ai_base_url: str = "",
        ai_model: str = "",
        engine: Optional[str] = None,
    ) -> PdfExtractResponse:
        """
        Read form field names and `/V` values. Optionally augment with
        pdfplumber-inferred labels (same path as `/template-fields` — no AI).

        When ai_labels=True, fields geometry couldn't label are sent to Gemini
        vision for a second pass (requires ai_api_key).
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

        from collections import OrderedDict

        # inspect rows grouped by their resolved AcroForm field name, IN ORDER.
        # A radio/checkbox group (Gender = Male/Female) yields several rows under
        # the same name — each is emitted as its own option row below.
        rows_by_name: "OrderedDict[str, list]" = OrderedDict()

        # Build a leaf→full-path index so pdfplumber labels (leaf names) can be
        # matched against raw_values keys that may be fully-qualified dotted paths
        # e.g. "F[0].P1[0].Producer_FullName_A[0]" → leaf "Producer_FullName_A[0]"
        def _leaf(name: str) -> str:
            return name.split(".")[-1] if "." in name else name

        leaf_to_full: Dict[str, str] = {_leaf(k): k for k in raw_values}

        if include_labels:
            from ..config import settings as _settings
            vision = VisionService(
                api_key=ai_api_key or "",
                base_url=ai_base_url or _settings.DEFAULT_AI_BASE_URL,
                model=ai_model or _settings.DEFAULT_AI_MODEL,
            )
            insp = vision.inspect_fillable_form(
                str(pdf_path), ai_labels=ai_labels, engine=engine
            )
            for row in insp.get("fields") or []:
                name = row.get("name")
                if not name:
                    continue
                # Prefer an EXACT match on the widget's fully-qualified name
                # (parent.child), which lines up 1:1 with raw_values keys. Fall
                # back to leaf-name matching only when the qualified name is
                # absent (non-acroform engines) or doesn't resolve. This is
                # essential for forms that RESTART leaf numbering inside each
                # parent (e.g. "Member Info T.0", "Diagnosis Info T.0",
                # "Signed Date T.0" all share leaf "0"): leaf matching collapses
                # them to one field (last-wins), blanking every other field and
                # mislabeling the survivor.
                qname = row.get("qualified_name")
                if qname and qname in raw_values:
                    full_name = qname
                else:
                    full_name = leaf_to_full.get(name, name)
                rows_by_name.setdefault(full_name, []).append(row)

        def _as_str(v) -> str:
            if isinstance(v, str):
                return v
            return str(v) if v is not None else ""

        def _norm_export(v) -> str:
            return _as_str(v).lstrip("/").strip()

        def _page_of(row) -> Optional[int]:
            # `row["page"]` is the 0-based page index from
            # _get_fields_with_coords; ExtractFieldItem.page is 1-based.
            raw_page = row.get("page")
            return int(raw_page) + 1 if raw_page is not None else 1

        fields: List[ExtractFieldItem] = []
        seen = set()
        for full_name, rows in rows_by_name.items():
            seen.add(full_name)
            current_val = _as_str(raw_values.get(full_name, ""))

            # A radio/checkbox GROUP is ONE AcroForm field (one /V) whose option
            # widgets share the name but carry DISTINCT export values (Male vs
            # Female). Emit each option as its own row so every choice is
            # visible; `value` echoes an option's export only when selected.
            exports = [r.get("export_value") for r in rows]
            is_group = (
                len(rows) > 1
                and all(exports)
                and len({e for e in exports}) >= 2
            )
            if is_group:
                cur = _norm_export(current_val)
                for r in rows:
                    exp = r.get("export_value")
                    opt_val = exp if (cur and _norm_export(exp) == cur) else ""
                    fields.append(
                        ExtractFieldItem(
                            name=full_name,
                            option=exp,
                            label=(r.get("label") or exp) if include_labels else None,
                            label_source=(r.get("label_source") or "geometry") if include_labels else None,
                            section=r.get("section") if include_labels else None,
                            subsection=r.get("subsection") if include_labels else None,
                            group=r.get("group") if include_labels else None,
                            table=r.get("table") if include_labels else None,
                            column=r.get("column") if include_labels else None,
                            value=opt_val,
                            page=_page_of(r),
                            field_type=r.get("field_type") or None,
                            linked_field=r.get("linked_field") if include_labels else None,
                            conditional=r.get("conditional") if include_labels else None,
                            skip_logic=r.get("skip_logic") if include_labels else None,
                            confidence=r.get("confidence") if include_labels else None,
                        )
                    )
            else:
                r = rows[-1]  # last-wins, mirroring prior behavior
                fields.append(
                    ExtractFieldItem(
                        name=full_name,
                        option=None,
                        label=(r.get("label") or full_name) if include_labels else None,
                        label_source=(r.get("label_source") or "geometry") if include_labels else None,
                        section=r.get("section") if include_labels else None,
                        subsection=r.get("subsection") if include_labels else None,
                        group=r.get("group") if include_labels else None,
                        table=r.get("table") if include_labels else None,
                        column=r.get("column") if include_labels else None,
                        value=current_val,
                        page=_page_of(r),
                        field_type=r.get("field_type") or None,
                        linked_field=r.get("linked_field") if include_labels else None,
                        conditional=r.get("conditional") if include_labels else None,
                        skip_logic=r.get("skip_logic") if include_labels else None,
                        confidence=r.get("confidence") if include_labels else None,
                    )
                )

        # Any raw AcroForm fields the inspector didn't report (or when
        # include_labels is False) — appended in stable sorted order, label-less.
        for name in sorted(raw_values.keys()):
            if name in seen:
                continue
            seen.add(name)
            fields.append(
                ExtractFieldItem(
                    name=name,
                    option=None,
                    label=None,
                    label_source=None,
                    section=None,
                    group=None,
                    table=None,
                    value=_as_str(raw_values.get(name, "")),
                    page=None,
                    field_type=None,
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
