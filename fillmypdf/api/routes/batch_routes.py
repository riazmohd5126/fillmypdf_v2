"""
Batch Fill API Routes
=====================
Endpoints for batch PDF processing
"""

import uuid
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, BackgroundTasks, Depends
from fastapi.responses import FileResponse
import json

from ...services.batch_fill_service import BatchFillService
from ...services.template_cache import TemplateCache
from ...services.ai_provider import prepare_ai_config
from ...config import settings
from ..dependencies.auth import require_api_key, require_admin
from ...models import FormTemplateInspectionResponse
from ..openapi_form_examples import (
    EX_AI_API_KEY,
    EX_AI_BASE_URL,
    EX_AI_MODEL,
    EX_JSON_RECORDS_TWO,
    EX_PROFILE_ID,
)


router = APIRouter(
    prefix="/batch",
    tags=["batch"],
    dependencies=[Depends(require_api_key)],
)

batch_service = BatchFillService()


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_batch_dir(batch_dir: Path):
    """Background task to cleanup batch directory"""
    try:
        if batch_dir.exists():
            shutil.rmtree(batch_dir)
    except Exception as e:
        print(f"Cleanup error: {e}")


@router.post(
    "/template-fields",
    response_model=FormTemplateInspectionResponse,
    summary="Inspect template fields (Layer 3 — no AI)",
)
async def inspect_template_fields(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF template (static PDF is converted like batch)"),
):
    """
    List fillable AcroForm fields inferred from this template plus on-page labels.

    Runs the same ``commonforms`` conversion step as batch fill, then uses
    pdfplumber proximity to guess each field's printed label — **without**
    sending anything to your LLM. Use before batch jobs or in a Chrome
    extension "preview detected fields" step.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Template must be a PDF file")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    template_path = settings.UPLOAD_DIR / f"{timestamp}_{uuid.uuid4().hex[:10]}_inspect_template.pdf"

    try:
        with open(template_path, "wb") as f:
            f.write(await file.read())

        payload = batch_service.analyze_template_fields(template_path)
        if not payload["success"]:
            raise HTTPException(
                status_code=400,
                detail=payload.get("message") or "Could not analyse template PDF",
            )
        return FormTemplateInspectionResponse(**payload)

    finally:
        background_tasks.add_task(_unlink_if_exists, template_path)


@router.post("/fill-json")
async def batch_fill_json(
    file: UploadFile = File(..., description="PDF template to fill"),
    user_data_array: str = Form(
        ...,
        description="JSON array of data objects",
        examples=[EX_JSON_RECORDS_TWO],
    ),
    ai_api_key: Optional[str] = Form(
        None,
        description="AI provider API key (required for Gemini; omit when ai_provider='local')",
        examples=[EX_AI_API_KEY],
    ),
    ai_base_url: Optional[str] = Form(
        None,
        description="AI API base URL (leave blank to use server default)",
        examples=[EX_AI_BASE_URL],
    ),
    ai_model: Optional[str] = Form(
        None,
        description="AI model name (leave blank to use server default)",
        examples=[EX_AI_MODEL],
    ),
    ai_provider: Optional[str] = Form(
        None,
        description="LLM provider override: 'gemini' (cloud) or 'local' (on-prem Qwen via Ollama/vLLM). "
                    "Omit to use the server-level AI_PROVIDER setting.",
    ),
    dpi: int = Form(
        default=200,
        ge=150,
        le=300,
        description="Image DPI",
        examples=[200],
    ),
    profile_id: Optional[str] = Form(None, description="Single profile ID (legacy)", examples=[EX_PROFILE_ID]),
    profile_ids: Optional[str] = Form(None, description="Comma-separated profile IDs to merge (takes precedence over profile_id)"),
    background_tasks: BackgroundTasks = None,
):
    """
    Batch fill: Same PDF template + JSON array of data.

    Pass `profile_ids` (comma-separated) to merge multiple profiles — e.g. a
    patient profile and a provider profile — as base data for every record.

    **Local/HIPAA mode:** set `ai_provider=local` (or configure `AI_PROVIDER=local`
    server-side) to route all LLM calls to your on-prem Ollama/vLLM server.
    No `ai_api_key` is needed in that case.

    **Returns:** ZIP file with filled PDFs + batch_report.json
    """
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(400, "Template must be a PDF file")

    # Resolve provider (server default, per-request override, local vs cloud)
    try:
        resolved_key, resolved_url, resolved_model = prepare_ai_config(
            request_api_key=ai_api_key,
            request_base_url=ai_base_url,
            request_model=ai_model,
            provider_hint=ai_provider,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # Parse JSON array
    try:
        data_array = json.loads(user_data_array)
    except json.JSONDecodeError:
        raise HTTPException(400, "user_data_array must be valid JSON array")
    
    if not isinstance(data_array, list):
        raise HTTPException(400, "user_data_array must be a JSON array")
    
    if len(data_array) == 0:
        raise HTTPException(400, "user_data_array cannot be empty")
    
    if len(data_array) > 500:
        raise HTTPException(400, "Maximum 500 records per batch")
    
    # Generate job ID
    batch_id = f"batch_{uuid.uuid4().hex[:8]}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Save PDF template
    template_path = settings.UPLOAD_DIR / f"{timestamp}_{batch_id}_template.pdf"
    pdf_content = await file.read()
    with open(template_path, "wb") as f:
        f.write(pdf_content)
    
    try:
        # Process batch
        parsed_ids = [p.strip() for p in profile_ids.split(",") if p.strip()] if profile_ids else None
        result = batch_service.process_batch_json(
            template_pdf_path=template_path,
            user_data_array=data_array,
            ai_api_key=resolved_key,
            ai_base_url=resolved_url,
            ai_model=resolved_model,
            batch_id=batch_id,
            dpi=dpi,
            profile_id=profile_id,
            profile_ids=parsed_ids,
        )
        
        # Schedule cleanup
        if background_tasks:
            background_tasks.add_task(template_path.unlink, missing_ok=True)
            background_tasks.add_task(cleanup_batch_dir, Path(result["batch_dir"]))
        
        return {
            "success": result["successful"] > 0,
            "batch_id": result["batch_id"],
            "total_records": result["total_records"],
            "successful": result["successful"],
            "failed": result["failed"],
            "success_rate": result["success_rate"],
            "download_url": result["download_url"],
            "profile_used": profile_id,
            "message": f"Processed {result['total_records']} records",
        }
    
    except Exception as e:
        raise HTTPException(500, f"Batch processing failed: {str(e)}")


@router.post("/fill-csv")
async def batch_fill_csv(
    pdf_template: UploadFile = File(..., description="PDF template to fill"),
    csv_file: UploadFile = File(..., description="CSV file with data"),
    ai_api_key: Optional[str] = Form(
        None,
        description="AI provider API key (required for Gemini; omit when ai_provider='local')",
        examples=[EX_AI_API_KEY],
    ),
    ai_base_url: Optional[str] = Form(
        None,
        description="AI API base URL (leave blank to use server default)",
        examples=[EX_AI_BASE_URL],
    ),
    ai_model: Optional[str] = Form(
        None,
        description="AI model name (leave blank to use server default)",
        examples=[EX_AI_MODEL],
    ),
    ai_provider: Optional[str] = Form(
        None,
        description="'gemini' or 'local' — overrides server AI_PROVIDER setting for this request",
    ),
    dpi: int = Form(
        default=200,
        ge=150,
        le=300,
        description="Image DPI",
        examples=[200],
    ),
    profile_id: Optional[str] = Form(None, description="Single profile ID (legacy)", examples=[EX_PROFILE_ID]),
    profile_ids: Optional[str] = Form(None, description="Comma-separated profile IDs to merge (takes precedence over profile_id)"),
    background_tasks: BackgroundTasks = None,
):
    """
    CSV Batch: Upload PDF template + CSV → Get ZIP with filled PDFs.

    Pass `profile_ids` (comma-separated) to merge multiple profiles as base
    data for every row. Each CSV row overrides/extends the merged profile data.

    **Returns:** ZIP file with one PDF per CSV row + batch_report.json
    """
    if not pdf_template.filename.lower().endswith('.pdf'):
        raise HTTPException(400, "Template must be a PDF file")
    
    if not csv_file.filename.lower().endswith('.csv'):
        raise HTTPException(400, "Data file must be CSV format")

    try:
        resolved_key, resolved_url, resolved_model = prepare_ai_config(
            request_api_key=ai_api_key,
            request_base_url=ai_base_url,
            request_model=ai_model,
            provider_hint=ai_provider,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # Generate job ID
    batch_id = f"csv_{uuid.uuid4().hex[:8]}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Save PDF template
    template_path = settings.UPLOAD_DIR / f"{timestamp}_{batch_id}_template.pdf"
    pdf_content = await pdf_template.read()
    with open(template_path, "wb") as f:
        f.write(pdf_content)
    
    # Read CSV
    csv_content = await csv_file.read()
    
    try:
        # Process batch
        parsed_ids = [p.strip() for p in profile_ids.split(",") if p.strip()] if profile_ids else None
        result = batch_service.process_csv_batch(
            template_pdf_path=template_path,
            csv_content=csv_content,
            csv_filename=csv_file.filename,
            ai_api_key=resolved_key,
            ai_base_url=resolved_url,
            ai_model=resolved_model,
            batch_id=batch_id,
            dpi=dpi,
            profile_id=profile_id,
            profile_ids=parsed_ids,
        )
        
        # Schedule cleanup
        if background_tasks:
            background_tasks.add_task(template_path.unlink, missing_ok=True)
            background_tasks.add_task(cleanup_batch_dir, Path(result["batch_dir"]))
        
        cache_hits = sum(1 for r in result["results"] if r.get("cache_hit"))
        avg_conf_values = [r["avg_confidence"] for r in result["results"]
                           if r.get("avg_confidence") is not None]
        overall_avg_conf = (
            round(sum(avg_conf_values) / len(avg_conf_values), 3)
            if avg_conf_values else None
        )
        return {
            "success": result["successful"] > 0,
            "batch_id": result["batch_id"],
            "csv_filename": csv_file.filename,
            "total_rows": result["total_records"],
            "successful": result["successful"],
            "failed": result["failed"],
            "success_rate": result["success_rate"],
            "download_url": result["download_url"],
            "profile_used": profile_id,
            "cache_hits": cache_hits,
            "avg_confidence": overall_avg_conf,
            "message": f"Processed {result['total_records']} records from CSV",
        }

    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"CSV batch processing failed: {str(e)}")


@router.post("/fill-xlsx")
async def batch_fill_xlsx(
    pdf_template: UploadFile = File(..., description="PDF template to fill"),
    xlsx_file: UploadFile = File(..., description=".xlsx with header row"),
    ai_api_key: Optional[str] = Form(
        None,
        description="AI provider API key (required for Gemini; omit when ai_provider='local')",
        examples=[EX_AI_API_KEY],
    ),
    ai_base_url: Optional[str] = Form(
        None,
        description="AI API base URL (leave blank to use server default)",
        examples=[EX_AI_BASE_URL],
    ),
    ai_model: Optional[str] = Form(
        None,
        description="AI model name (leave blank to use server default)",
        examples=[EX_AI_MODEL],
    ),
    ai_provider: Optional[str] = Form(
        None,
        description="'gemini' or 'local' — overrides server AI_PROVIDER setting for this request",
    ),
    dpi: int = Form(
        default=200,
        ge=150,
        le=300,
        description="Image DPI",
        examples=[200],
    ),
    profile_id: Optional[str] = Form(None, description="Single profile ID (legacy)", examples=[EX_PROFILE_ID]),
    profile_ids: Optional[str] = Form(None, description="Comma-separated profile IDs to merge (takes precedence over profile_id)"),
    background_tasks: BackgroundTasks = None,
):
    """
    Same as CSV batch — first worksheet: row 1 = column names, subsequent rows =
    records. Pass `profile_ids` (comma-separated) to merge multiple profiles.
    Requires **openpyxl** (declared in `requirements.txt`).
    """
    fn = xlsx_file.filename.lower() if xlsx_file.filename else ""
    if not pdf_template.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Template must be a PDF file")
    if not (fn.endswith(".xlsx")):
        raise HTTPException(400, "Data file must be Excel .xlsx format")

    try:
        resolved_key, resolved_url, resolved_model = prepare_ai_config(
            request_api_key=ai_api_key,
            request_base_url=ai_base_url,
            request_model=ai_model,
            provider_hint=ai_provider,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    batch_id = f"xlsx_{uuid.uuid4().hex[:8]}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    template_path = settings.UPLOAD_DIR / f"{timestamp}_{batch_id}_template.pdf"

    pdf_content = await pdf_template.read()
    with open(template_path, "wb") as f:
        f.write(pdf_content)
    xlsx_content = await xlsx_file.read()

    try:
        parsed_ids = [p.strip() for p in profile_ids.split(",") if p.strip()] if profile_ids else None
        result = batch_service.process_xlsx_batch(
            template_pdf_path=template_path,
            xlsx_content=xlsx_content,
            xlsx_filename=xlsx_file.filename or "data.xlsx",
            ai_api_key=resolved_key,
            ai_base_url=resolved_url,
            ai_model=resolved_model,
            batch_id=batch_id,
            dpi=dpi,
            profile_id=profile_id,
            profile_ids=parsed_ids,
        )
        if background_tasks:
            background_tasks.add_task(_unlink_if_exists, template_path)
            background_tasks.add_task(cleanup_batch_dir, Path(result["batch_dir"]))

        cache_hits = sum(1 for r in result["results"] if r.get("cache_hit"))
        avg_vals = [
            r["avg_confidence"] for r in result["results"] if r.get("avg_confidence") is not None
        ]
        overall_avg_conf = round(sum(avg_vals) / len(avg_vals), 3) if avg_vals else None
        return {
            "success": result["successful"] > 0,
            "batch_id": result["batch_id"],
            "xlsx_filename": xlsx_file.filename,
            "total_rows": result["total_records"],
            "successful": result["successful"],
            "failed": result["failed"],
            "success_rate": result["success_rate"],
            "download_url": result["download_url"],
            "profile_used": profile_id,
            "cache_hits": cache_hits,
            "avg_confidence": overall_avg_conf,
            "message": f"Processed {result['total_records']} records from Excel",
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Excel batch processing failed: {str(e)}")


@router.get("/download/{filename}")
async def download_batch_result(filename: str):
    """Serve artifacts stored in OUTPUT_DIR (batch ZIP or async extract JSON/CSV)."""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    file_path = settings.OUTPUT_DIR / filename

    if not file_path.exists():
        raise HTTPException(404, "File not found")

    suffix = file_path.suffix.lower()
    media = {
        ".zip": "application/zip",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".csv": "text/csv; charset=utf-8",
    }.get(suffix)
    if not media:
        raise HTTPException(
            400, "Unsupported file type — expecting .zip, .pdf, .json, or .csv",
        )

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Cache admin endpoints (admin key required)
# ---------------------------------------------------------------------------

@router.get(
    "/cache",
    summary="List template mapping cache entries (admin)",
    dependencies=[Depends(require_admin)],
)
async def list_cache_entries():
    """
    List all cached AI field→value mapping templates.

    Each entry shows the template fingerprint, when it was cached, and how
    many fields were mapped. Requires an **admin** API key.
    """
    cache = TemplateCache()
    return {"entries": cache.list_entries(), "total": len(cache.list_entries())}


@router.delete(
    "/cache/{fingerprint}",
    status_code=204,
    summary="Invalidate one cache entry (admin)",
    dependencies=[Depends(require_admin)],
)
async def invalidate_cache_entry(fingerprint: str):
    """
    Delete a single cached template mapping by its 32-char fingerprint.

    Use this after significantly changing the form layout so the next fill
    re-runs the AI instead of using the stale cached mappings. Requires
    an **admin** API key.
    """
    cache = TemplateCache()
    if not cache.invalidate(fingerprint):
        raise HTTPException(404, f"Cache entry '{fingerprint}' not found")
    return None


@router.delete(
    "/cache",
    status_code=204,
    summary="Clear entire mapping cache (admin)",
    dependencies=[Depends(require_admin)],
)
async def clear_cache():
    """
    Remove **all** cached template mappings.

    The next fill of any template will call the AI fresh. Requires an
    **admin** API key.
    """
    cache = TemplateCache()
    cleared = 0
    for entry in cache.list_entries():
        if cache.invalidate(entry["fingerprint"]):
            cleared += 1
    return None
