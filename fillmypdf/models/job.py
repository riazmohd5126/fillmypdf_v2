"""
Async Job Models
=================
Represents a long-running PDF batch fill job that runs in the background.

States:
  queued   → submitted, not yet picked up by a worker
  running  → actively processing records
  done     → all records processed (some may have failed)
  failed   → job-level error before any records were processed
  cancelled→ cancelled by the caller
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

_JOB_SUMMARY_SCHEMA_EXAMPLE = {
    "id": "job_a1b2c3d4e5f6",
    "status": "done",
    "kind": "batch_fill",
    "template_id": None,
    "record_count": 2,
    "progress_pct": 100.0,
    "completed": 2,
    "successful": 2,
    "failed": 0,
    "created_at": "2026-05-09T12:00:00+00:00",
    "started_at": "2026-05-09T12:00:01+00:00",
    "completed_at": "2026-05-09T12:03:45+00:00",
    "download_url": "/api/v1/batch/download/job_a1b2c3d4e5f6.zip",
    "avg_confidence": 0.94,
    "cache_hits": 1,
    "error": None,
    "webhook_url": "https://hooks.example.invalid/fillmypdf",
    "webhook_delivered": True,
}


JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]

JobKindFilter = Literal["batch_fill", "batch_fill_xlsx", "template_fill", "extract_pdf"]


class JobProgress(BaseModel):
    total: int = 0
    completed: int = 0
    successful: int = 0
    failed: int = 0

    @property
    def pct(self) -> float:
        if not self.total:
            return 0.0
        return round(self.completed / self.total * 100, 1)


class Job(BaseModel):
    """Full job record — persisted to disk as {job_id}.json."""
    id: str
    status: JobStatus = "queued"
    kind: JobKindFilter = "batch_fill"

    # Who submitted it
    api_key_id: Optional[str] = None

    # What to process
    template_id: Optional[str] = None   # set when kind == "template_fill"
    record_count: int = 0

    # Progress
    progress: JobProgress = Field(default_factory=JobProgress)

    # Timing
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    # Result
    download_url: Optional[str] = None
    avg_confidence: Optional[float] = None
    cache_hits: int = 0

    # Error (job-level, not per-record)
    error: Optional[str] = None

    # Webhook
    webhook_url: Optional[str] = None
    webhook_delivered: bool = False
    webhook_error: Optional[str] = None

    # Opaque payload used by the runner to (re-)start the job.
    # Stored separately in {job_id}_payload.json to keep the main record small.
    # This field is never returned to callers.
    _has_payload: bool = False

    custom: Dict[str, Any] = Field(default_factory=dict)


class JobSummary(BaseModel):
    """Lightweight view returned by GET /jobs and GET /jobs/{id}."""

    model_config = ConfigDict(
        json_schema_extra={"example": _JOB_SUMMARY_SCHEMA_EXAMPLE}
    )

    id: str
    status: JobStatus
    kind: str
    template_id: Optional[str] = None
    record_count: int
    progress_pct: float
    completed: int
    successful: int
    failed: int
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    download_url: Optional[str] = None
    avg_confidence: Optional[float] = None
    cache_hits: int = 0
    error: Optional[str] = None
    webhook_url: Optional[str] = None
    webhook_delivered: bool = False


class JobListResponse(BaseModel):
    jobs: List[JobSummary]
    total: int

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "jobs": [_JOB_SUMMARY_SCHEMA_EXAMPLE],
                "total": 1,
            }
        }
    )


class JobSubmitResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str
    status_url: str

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "job_a1b2c3d4e5f6",
                "status": "queued",
                "message": "Job queued — 2 records",
                "status_url": "/api/v1/jobs/job_a1b2c3d4e5f6",
            }
        }
    )


class WebhookRedeliveryResponse(BaseModel):
    job_id: str
    message: str

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "job_a1b2c3d4e5f6",
                "message": "Completion webhook queued (delivered asynchronously).",
            }
        }
    )
