"""
Async Job Queue API Routes
===========================
Submit long-running batch fills and poll their status.

Flow for a caller:
  1. POST /api/v1/jobs/batch | /template-batch | /xlsx-batch | /extract
     → immediate 202 response with {job_id, status_url}
  2. GET  /api/v1/jobs/{job_id}
     → poll until status == "done" or "failed"
  2b. GET  /api/v1/jobs?limit=&status=&kind=
     → recent jobs (newest first); optional filters by status and job kind
  3. GET  /api/v1/jobs/{job_id}/download
     → redirect to the ZIP / PDF (same as download_url in the status payload)

Optional webhook: pass ``webhook_url``; completion is a JSON POST. If env
``WEBHOOK_SIGNING_SECRET`` **or** multipart ``webhook_secret`` is set, the POST
also includes ``X-FillMyPDF-Timestamp`` and ``X-FillMyPDF-Signature`` (hex HMAC-v1).

After a terminal job, use ``POST /jobs/{job_id}/retry-webhook`` to queue another
delivery (same signing and retry/backoff as the automatic POST).

Body fields align with polling ``GET /api/v1/jobs/{job_id}`` (``kind``,
``progress_pct``, totals, timestamps, legacy ``successful`` / ``failed``).
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
import json

from ...config import settings
from ...models.job import (
    JobKindFilter,
    JobListResponse,
    JobSubmitResponse,
    JobSummary,
    JobStatus,
    WebhookRedeliveryResponse,
)
from ...repositories.job_repository import JobRepository
from ...services.job_runner import get_runner
from ...services.ai_provider import prepare_ai_config
from ..dependencies.auth import require_api_key, get_current_key_id
from ..openapi_form_examples import (
    EX_AI_API_KEY,
    EX_AI_BASE_URL,
    EX_AI_MODEL,
    EX_JSON_RECORDS_TWO,
    EX_PROFILE_ID,
    EX_TEMPLATE_ID,
    EX_WEBHOOK_SECRET,
    EX_WEBHOOK_URL,
)

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_api_key)],
)


def _repo() -> JobRepository:
    return JobRepository()


# ---------------------------------------------------------------------------
# Submit: ad-hoc batch (caller uploads the template PDF)
# ---------------------------------------------------------------------------


@router.post(
    "/batch",
    response_model=JobSubmitResponse,
    status_code=202,
    summary="Submit async batch fill (upload PDF)",
)
async def submit_batch_job(
    request: Request,
    file: UploadFile = File(..., description="PDF template"),
    records: str = Form(
        ...,
        description="JSON array of patient records",
        examples=[EX_JSON_RECORDS_TWO],
    ),
    ai_api_key: Optional[str] = Form(
        None,
        description="AI provider API key (required for Gemini; omit when ai_provider='local')",
        examples=[EX_AI_API_KEY],
    ),
    ai_base_url: str = Form(
        default=EX_AI_BASE_URL,
        examples=[EX_AI_BASE_URL],
    ),
    ai_model: str = Form(
        default="gemini-2.5-flash",
        examples=[EX_AI_MODEL],
    ),
    ai_provider: Optional[str] = Form(
        None,
        description="'gemini' or 'local' — overrides server AI_PROVIDER for this request",
    ),
    dpi: int = Form(default=200, ge=150, le=300, examples=[200]),
    profile_id: Optional[str] = Form(None, examples=[EX_PROFILE_ID]),
    profile_ids: Optional[str] = Form(None, description="Comma-separated profile IDs to merge (takes precedence over profile_id)"),
    webhook_url: Optional[str] = Form(
        None,
        description="URL to POST a completion event to (optional)",
        examples=[EX_WEBHOOK_URL],
    ),
    webhook_secret: Optional[str] = Form(
        None,
        description="Secret for X-FillMyPDF-Signature (overrides WEBHOOK_SIGNING_SECRET)",
        examples=[EX_WEBHOOK_SECRET],
    ),
):
    """
    Submit a batch fill job and return **immediately** with a job ID.

    The PDF is stored server-side; records are processed in the background.
    Poll `GET /api/v1/jobs/{job_id}` for progress.  When `status == 'done'`
    use the `download_url` to fetch the ZIP.

    Pass `profile_ids` (comma-separated) to merge multiple profiles as base
    data for every record. Optional: pass `webhook_url` for a POST notification.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Template must be a PDF file")

    try:
        data_list = json.loads(records)
        if not isinstance(data_list, list) or not data_list:
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "records must be a non-empty JSON array")

    if len(data_list) > 500:
        raise HTTPException(400, "Maximum 500 records per job")

    pdf_bytes = await file.read()
    key_id = get_current_key_id(request)
    parsed_ids = [p.strip() for p in profile_ids.split(",") if p.strip()] if profile_ids else None

    try:
        resolved_key, resolved_url, resolved_model = prepare_ai_config(
            request_api_key=ai_api_key,
            request_base_url=ai_base_url,
            request_model=ai_model,
            provider_hint=ai_provider,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    job = get_runner().submit_batch(
        records=data_list,
        template_pdf_bytes=pdf_bytes,
        template_filename=file.filename,
        ai_api_key=resolved_key,
        ai_base_url=resolved_url,
        ai_model=resolved_model,
        dpi=dpi,
        profile_id=profile_id,
        profile_ids=parsed_ids,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        api_key_id=key_id,
    )

    return JobSubmitResponse(
        job_id=job.id,
        status=job.status,
        message=f"Job queued — {len(data_list)} records",
        status_url=f"/api/v1/jobs/{job.id}",
    )


# ---------------------------------------------------------------------------
# Submit: template-library batch (no PDF upload needed)
# ---------------------------------------------------------------------------


@router.post(
    "/template-batch",
    response_model=JobSubmitResponse,
    status_code=202,
    summary="Submit async batch fill against a stored template",
)
async def submit_template_batch_job(
    request: Request,
    template_id: str = Form(
        ...,
        description="Template ID from the library",
        examples=[EX_TEMPLATE_ID],
    ),
    records: str = Form(
        ...,
        description="JSON array of patient records",
        examples=[EX_JSON_RECORDS_TWO],
    ),
    ai_api_key: Optional[str] = Form(
        None,
        description="AI provider API key (required for Gemini; omit when ai_provider='local')",
        examples=[EX_AI_API_KEY],
    ),
    ai_base_url: str = Form(
        default=EX_AI_BASE_URL,
        examples=[EX_AI_BASE_URL],
    ),
    ai_model: str = Form(
        default="gemini-2.5-flash",
        examples=[EX_AI_MODEL],
    ),
    ai_provider: Optional[str] = Form(
        None,
        description="'gemini' or 'local' — overrides server AI_PROVIDER for this request",
    ),
    dpi: int = Form(default=200, ge=150, le=300, examples=[200]),
    profile_id: Optional[str] = Form(None, examples=[EX_PROFILE_ID]),
    profile_ids: Optional[str] = Form(None, description="Comma-separated profile IDs to merge (takes precedence over profile_id)"),
    webhook_url: Optional[str] = Form(None, examples=[EX_WEBHOOK_URL]),
    webhook_secret: Optional[str] = Form(
        None,
        description="Webhook HMAC secret (overrides WEBHOOK_SIGNING_SECRET)",
        examples=[EX_WEBHOOK_SECRET],
    ),
):
    """
    Submit a batch fill against a **stored library template** and return immediately.

    No PDF upload required — just the template ID. Pass `profile_ids`
    (comma-separated) to merge multiple profiles (e.g. patient + provider).
    """
    try:
        data_list = json.loads(records)
        if not isinstance(data_list, list) or not data_list:
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "records must be a non-empty JSON array")

    if len(data_list) > 500:
        raise HTTPException(400, "Maximum 500 records per job")

    key_id = get_current_key_id(request)
    parsed_ids = [p.strip() for p in profile_ids.split(",") if p.strip()] if profile_ids else None

    try:
        resolved_key, resolved_url, resolved_model = prepare_ai_config(
            request_api_key=ai_api_key,
            request_base_url=ai_base_url,
            request_model=ai_model,
            provider_hint=ai_provider,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    job = get_runner().submit_template_batch(
        template_id=template_id,
        records=data_list,
        ai_api_key=resolved_key,
        ai_base_url=resolved_url,
        ai_model=resolved_model,
        dpi=dpi,
        profile_id=profile_id,
        profile_ids=parsed_ids,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        api_key_id=key_id,
    )

    return JobSubmitResponse(
        job_id=job.id,
        status=job.status,
        message=f"Job queued — {len(data_list)} records against template '{template_id}'",
        status_url=f"/api/v1/jobs/{job.id}",
    )


# ---------------------------------------------------------------------------
# Submit: Excel workbook + PDF template (same rows as synchronous /batch/fill-xlsx)
# ---------------------------------------------------------------------------


@router.post(
    "/xlsx-batch",
    response_model=JobSubmitResponse,
    status_code=202,
    summary="Submit async batch fill from Excel (.xlsx) + PDF",
)
async def submit_xlsx_job(
    request: Request,
    file: UploadFile = File(..., description="PDF template"),
    xlsx_file: UploadFile = File(..., description="Excel .xlsx (header row + data)"),
    ai_api_key: Optional[str] = Form(
        None,
        description="AI provider API key (required for Gemini; omit when ai_provider='local')",
        examples=[EX_AI_API_KEY],
    ),
    ai_base_url: str = Form(
        default=EX_AI_BASE_URL,
        examples=[EX_AI_BASE_URL],
    ),
    ai_model: str = Form(
        default="gemini-2.5-flash",
        examples=[EX_AI_MODEL],
    ),
    ai_provider: Optional[str] = Form(
        None,
        description="'gemini' or 'local' — overrides server AI_PROVIDER for this request",
    ),
    dpi: int = Form(default=200, ge=150, le=300, examples=[200]),
    profile_id: Optional[str] = Form(None, examples=[EX_PROFILE_ID]),
    profile_ids: Optional[str] = Form(None, description="Comma-separated profile IDs to merge (takes precedence over profile_id)"),
    webhook_url: Optional[str] = Form(None, examples=[EX_WEBHOOK_URL]),
    webhook_secret: Optional[str] = Form(
        None,
        description="Webhook HMAC secret (overrides WEBHOOK_SIGNING_SECRET)",
        examples=[EX_WEBHOOK_SECRET],
    ),
):
    """Queue a spreadsheet batch job and return immediately with a ``job_id``."""
    fn = xlsx_file.filename.lower() if xlsx_file.filename else ""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Template must be a PDF file")
    if not fn.endswith(".xlsx"):
        raise HTTPException(400, "Data file must be .xlsx format")

    pdf_bytes = await file.read()
    xlsx_bytes = await xlsx_file.read()
    parsed_ids = [p.strip() for p in profile_ids.split(",") if p.strip()] if profile_ids else None

    try:
        resolved_key, resolved_url, resolved_model = prepare_ai_config(
            request_api_key=ai_api_key,
            request_base_url=ai_base_url,
            request_model=ai_model,
            provider_hint=ai_provider,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    try:
        job = get_runner().submit_xlsx_batch(
            template_pdf_bytes=pdf_bytes,
            xlsx_bytes=xlsx_bytes,
            xlsx_filename=xlsx_file.filename or "data.xlsx",
            ai_api_key=resolved_key,
            ai_base_url=resolved_url,
            ai_model=resolved_model,
            dpi=dpi,
            profile_id=profile_id,
            profile_ids=parsed_ids,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            api_key_id=get_current_key_id(request),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return JobSubmitResponse(
        job_id=job.id,
        status=job.status,
        message=f"Excel job queued — {job.record_count} rows",
        status_url=f"/api/v1/jobs/{job.id}",
    )


# ---------------------------------------------------------------------------
# Submit: PDF field extraction → JSON / CSV artifact (optional webhook)
# ---------------------------------------------------------------------------


@router.post(
    "/extract",
    response_model=JobSubmitResponse,
    status_code=202,
    summary="Submit async PDF form-field extraction",
)
async def submit_extract_job(
    request: Request,
    file: UploadFile = File(..., description="Filled or fillable PDF"),
    include_labels: bool = Form(
        True,
        description="pdfplumber-inferred labels merged per field when true",
        examples=[True],
    ),
    output_format: Literal["json", "csv"] = Form(
        "json",
        description="Downloadable artifact type when status is done",
        examples=["json"],
    ),
    webhook_url: Optional[str] = Form(
        None,
        description="URL to POST completion event to",
        examples=[EX_WEBHOOK_URL],
    ),
    webhook_secret: Optional[str] = Form(
        None,
        description="Webhook HMAC secret (overrides WEBHOOK_SIGNING_SECRET)",
        examples=[EX_WEBHOOK_SECRET],
    ),
):
    """
    Queue **AcroForm extraction** — same semantics as synchronous ``POST /extract``,
    but the result lands as ``.json`` or ``.csv`` under ``OUTPUT_DIR``.
    Poll ``GET /jobs/{id}``, then fetch via ``download_url``
    or ``GET /jobs/{id}/download``.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "File must be a PDF")

    pdf_bytes = await file.read()

    try:
        job = get_runner().submit_extract_pdf(
            pdf_bytes=pdf_bytes,
            source_filename=file.filename or "extract.pdf",
            include_labels=include_labels,
            output_format=output_format,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            api_key_id=get_current_key_id(request),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    artifact = output_format.upper()
    return JobSubmitResponse(
        job_id=job.id,
        status=job.status,
        message=f"Extract job queued ({artifact}); poll status_url until done.",
        status_url=f"/api/v1/jobs/{job.id}",
    )


# ---------------------------------------------------------------------------
# Get job status
# ---------------------------------------------------------------------------


@router.get("/{job_id}", response_model=JobSummary, summary="Get job status")
async def get_job(job_id: str):
    """
    Poll job status.

    `progress_pct` goes from 0 → 100 while the job runs.
    When `status == 'done'`, use `download_url` to fetch the filled ZIP.
    When `status == 'failed'`, see `error` for the reason.
    """
    repo = _repo()
    job = repo.get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found")
    return repo.to_summary(job)


@router.post(
    "/{job_id}/retry-webhook",
    response_model=WebhookRedeliveryResponse,
    status_code=202,
    summary="Re-queue completion webhook",
)
async def retry_job_webhook(job_id: str):
    """
    Queue another POST to ``webhook_url`` with the latest job snapshot.

    Only for terminal jobs (**done**, **failed**, or **cancelled**).
    Requires the job was originally submitted with ``webhook_url``.
    Uses the same HMAC signing and exponential retry policy as automatic delivery.
    """
    repo = _repo()
    job = repo.get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found")
    if job.status in ("queued", "running"):
        raise HTTPException(
            409,
            f"Job '{job_id}' is still {job.status}; webhook reflects final state only.",
        )
    if not job.webhook_url:
        raise HTTPException(400, f"Job '{job_id}' has no webhook_url configured")

    get_runner().enqueue_webhook_redelivery(job_id)
    return WebhookRedeliveryResponse(
        job_id=job_id,
        message="Completion webhook queued (delivered asynchronously).",
    )


# ---------------------------------------------------------------------------
# Download shortcut
# ---------------------------------------------------------------------------


@router.get("/{job_id}/download", summary="Download job result (redirect)")
async def download_job_result(job_id: str):
    """
    Redirect to the filled ZIP / PDF once the job is done.

    Returns 404 if the job doesn't exist, 409 if still running.
    """
    repo = _repo()
    job = repo.get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found")
    if job.status in ("queued", "running"):
        raise HTTPException(409, f"Job '{job_id}' is still {job.status}")
    if job.status in ("failed", "cancelled"):
        raise HTTPException(410, f"Job '{job_id}' {job.status}: {job.error}")
    if not job.download_url:
        raise HTTPException(404, "Job result file not available")

    # Reuse the existing batch/template download endpoints
    filename = job.download_url.split("/")[-1]
    path = settings.OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(410, "Result file has been cleaned up")

    from fastapi.responses import FileResponse

    suffix = path.suffix.lower()
    media = {
        ".zip": "application/zip",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".csv": "text/csv; charset=utf-8",
    }.get(suffix, "application/octet-stream")

    return FileResponse(
        path=str(path),
        filename=filename,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# List recent jobs
# ---------------------------------------------------------------------------


@router.get("", response_model=JobListResponse, summary="List recent jobs")
async def list_jobs(
    limit: int = Query(50, ge=1, description="Maximum jobs to return (capped server-side)."),
    status: Optional[JobStatus] = Query(
        None,
        description='Filter by lifecycle status (omit for all statuses).',
    ),
    kind: Optional[JobKindFilter] = Query(
        None,
        description='Filter by job kind (`batch_fill`, `batch_fill_xlsx`, `template_fill`, `extract_pdf`).',
    ),
):
    """List recent jobs (**newest first**), optionally filtered by ``status`` and/or ``kind``."""
    limit = min(limit, settings.JOB_MAX_LISTED)
    repo = _repo()
    jobs = repo.list_recent(limit=limit, status=status, kind=kind)
    return JobListResponse(
        jobs=[repo.to_summary(j) for j in jobs],
        total=len(jobs),
    )


# ---------------------------------------------------------------------------
# Cancel / delete
# ---------------------------------------------------------------------------


@router.delete(
    "/{job_id}",
    status_code=204,
    summary="Cancel or delete a job",
)
async def cancel_job(job_id: str):
    """
    Cancel a queued job or delete the record of a completed one.

    Running jobs are marked `cancelled` but the in-progress worker is not
    interrupted (it will finish its current record before stopping).
    """
    repo = _repo()
    job = repo.get(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found")

    if job.status in ("queued",):
        repo.update_status(job_id, status="cancelled")
    else:
        repo.delete(job_id)
    return None
