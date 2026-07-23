"""
Smart Data Extraction API
==========================
Extract AcroForm field values + optional inferred labels into JSON or CSV.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

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
    ),
    ai_labels: bool = Query(
        default=False,
        description=(
            "Use Gemini vision to label fields geometry couldn't resolve "
            "(fields with cryptic names like 'undefined_3'). "
            "Requires ai_api_key or GEMINI_API_KEY env var."
        ),
    ),
    ai_api_key: str = Query(
        default="",
        description="Gemini API key for ai_labels mode. Falls back to GEMINI_API_KEY env.",
    ),
    engine: Optional[Literal["opencv", "vlm_local", "acroform", "gemini"]] = Query(
        default=None,
        description=(
            "Field-detection engine. 'opencv' (local OpenCV box detection, no AI), "
            "'vlm_local' (local Qwen-VL understanding, never egresses), "
            "'acroform' (pypdf widgets + pdfplumber geometry), "
            "'gemini' (full cloud pass: whole page + every field sent to Gemini "
            "for label/section/group). Defaults to server FIELD_DETECTION_ENGINE."
        ),
    ),
    fmt: Literal["json", "csv"] = Query(
        default="json",
        alias="format",
        description="Structured JSON response or downloadable CSV",
    ),
):
    """
    Reverse of fill — read `/V` values from every AcroForm widget into JSON.

    Optionally includes **printed labels** next to fields via lane-constrained
    geometry (free, no AI). Set `ai_labels=true` to additionally resolve
    leftover unresolved fields using Gemini vision.

    CSV returns rows `name,label,label_source,section,group,table,value,page,field_type`.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "File must be a PDF")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uploaded = settings.UPLOAD_DIR / f"{ts}_{uuid.uuid4().hex[:8]}_extract.pdf"
    uploaded.write_bytes(await file.read())

    # Resolve which detection engine to use (per-request > server default)
    eff_engine = (engine or settings.FIELD_DETECTION_ENGINE or "acroform").lower()

    # Resolve Gemini key: query param > server setting > env var
    resolved_key = (
        (ai_api_key or "").strip()
        or (settings.GEMINI_API_KEY or "").strip()
        or os.getenv("GEMINI_API_KEY", "")
    )
    ai_base_url = settings.DEFAULT_AI_BASE_URL
    ai_model = settings.DEFAULT_AI_MODEL

    # When the caller didn't force an engine and a Gemini key is available (and
    # not in HIPAA local-only mode), use the FULL Gemini pass. It labels every
    # field and caches the result by form structure, so it costs one call per
    # template and reads locally forever after.
    if (
        engine is None
        and eff_engine == "acroform"
        and resolved_key
        and not settings.AI_LOCAL_ONLY
        and settings.AI_LABEL_FALLBACK
    ):
        eff_engine = "gemini"

    # "Use Gemini where AcroForm can't label a field" — when a server Gemini
    # key is configured and we're not in HIPAA local-only mode, the acroform
    # engine AUTOMATICALLY sends its unresolved (label_source=="name") fields
    # to Gemini vision, without the caller passing ai_labels/ai_api_key.
    auto_fallback = (
        settings.AI_LABEL_FALLBACK
        and eff_engine == "acroform"
        and bool(resolved_key)
        and not settings.AI_LOCAL_ONLY
    )
    use_ai_labels = (ai_labels or auto_fallback) and bool(resolved_key) and eff_engine == "acroform"

    if eff_engine == "vlm_local":
        # Local-only vision config; hard-pinned to a private host (no Gemini).
        from ...services.ai_provider import prepare_local_vision_config
        try:
            resolved_key, ai_base_url, ai_model = prepare_local_vision_config()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    elif eff_engine == "gemini":
        # Full cloud pass — a Gemini key is mandatory.
        if not resolved_key:
            raise HTTPException(
                400,
                "engine=gemini requires a Gemini API key. "
                "Pass ai_api_key=, set GEMINI_API_KEY, or configure it in settings."
            )
    elif ai_labels and not resolved_key:
        raise HTTPException(
            400,
            "ai_labels=true requires a Gemini API key. "
            "Pass ai_api_key=, set GEMINI_API_KEY, or configure it in settings."
        )

    # Honour the HIPAA egress guardrail: if AI_LOCAL_ONLY blocks Gemini's host,
    # refuse rather than silently leaking PHI. Applies to any cloud-Gemini call
    # (the auto acroform fallback, forced ai_labels, or the full gemini engine).
    if use_ai_labels or eff_engine == "gemini":
        from ...services.ai_provider import assert_egress_allowed
        try:
            assert_egress_allowed(ai_base_url)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    try:
        result = _svc().extract_pdf(
            uploaded,
            include_labels=include_labels,
            ai_labels=use_ai_labels,
            ai_api_key=resolved_key,
            ai_base_url=ai_base_url,
            ai_model=ai_model,
            engine=eff_engine,
        )
        result = result.model_copy(update={"filename": file.filename})

        if fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(
                buf,
                fieldnames=["name", "label", "label_source", "section", "group", "table", "value", "page", "field_type"],
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
