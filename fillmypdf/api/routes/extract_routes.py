"""
Smart Data Extraction API
==========================
Extract AcroForm field values + optional inferred labels into JSON or CSV.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Literal

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from ...models.extract import PdfExtractResponse
from ...services.extraction_service import ExtractionService
from ...config import settings
from ..dependencies.auth import require_api_key

router = APIRouter(
    prefix="/extract",
    tags=["extract"],
    dependencies=[Depends(require_api_key)],
)


def _svc() -> ExtractionService:
    return ExtractionService()


def _unlink(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


@router.post(
    "",
    response_model=None,
    summary="Extract form field values from PDF",
)
async def extract_pdf_fields(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Filled or fillable PDF"),
    include_labels: bool = Query(
        default=True,
        description="Merge pdfplumber-inferred labels per field (no AI cost)",
        examples=[True, False],
    ),
    fmt: Literal["json", "csv"] = Query(
        default="json",
        alias="format",
        description="Structured JSON response or downloadable CSV",
        examples=["json", "csv"],
    ),
):
    """
    Reverse of fill — read `/V` values from every AcroForm widget into JSON.

    Optionally includes **printed labels** next to fields (same engine as the
    template-inspection preview). This does **not** call Gemini.

    CSV returns rows `name,label,value,page,field_type`.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "File must be a PDF")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uploaded = settings.UPLOAD_DIR / f"{ts}_{uuid.uuid4().hex[:8]}_extract.pdf"
    uploaded.write_bytes(await file.read())

    try:
        result = _svc().extract_pdf(uploaded, include_labels=include_labels)
        result = result.model_copy(update={"filename": file.filename})

        if fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(
                buf,
                fieldnames=["name", "label", "value", "page", "field_type"],
                extrasaction="ignore",
            )
            w.writeheader()
            for row in result.fields:
                w.writerow(row.model_dump())
            fn = Path(file.filename or "extract").stem + "_extract.csv"
            body = ("\ufeff" + buf.getvalue()).encode("utf-8")
            return Response(
                content=body,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{fn}"'},
            )
        return result
    finally:
        background_tasks.add_task(_unlink, uploaded)
