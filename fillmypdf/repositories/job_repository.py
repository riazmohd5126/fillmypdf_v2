"""
Job Repository
==============
Thread-safe disk persistence for async jobs.

Layout::

    {STORAGE_DIR}/jobs/
        {job_id}.json          ← Job record (status, progress, result …)
        {job_id}_payload.json  ← Large input payload (records, ai key, …)

The job record is small and read frequently; the payload is read once by the
worker and never returned to callers.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import settings
from ..models.job import Job, JobKindFilter, JobSummary, JobStatus


class JobRepository:
    """Disk-backed, thread-safe job store."""

    _lock = threading.Lock()   # one global write-lock is fine for our scale

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def jobs_dir(self) -> Path:
        p = settings.STORAGE_DIR / "jobs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _payload_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}_payload.json"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(self, job: Job) -> Job:
        with self._lock:
            self._job_path(job.id).write_text(job.model_dump_json(indent=2))
        return job

    def save_payload(self, job_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._payload_path(job_id).write_text(json.dumps(payload))

    def update_status(
        self,
        job_id: str,
        *,
        status: str,
        error: Optional[str] = None,
        download_url: Optional[str] = None,
        avg_confidence: Optional[float] = None,
        cache_hits: Optional[int] = None,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        progress_completed: Optional[int] = None,
        progress_successful: Optional[int] = None,
        progress_failed: Optional[int] = None,
        webhook_delivered: Optional[bool] = None,
        webhook_error: Optional[str] = None,
    ) -> Optional[Job]:
        with self._lock:
            job = self._read_locked(job_id)
            if job is None:
                return None
            job.status = status  # type: ignore[assignment]
            if error is not None:
                job.error = error
            if download_url is not None:
                job.download_url = download_url
            if avg_confidence is not None:
                job.avg_confidence = avg_confidence
            if cache_hits is not None:
                job.cache_hits = cache_hits
            if started_at is not None:
                job.started_at = started_at
            if completed_at is not None:
                job.completed_at = completed_at
            if progress_completed is not None:
                job.progress.completed = progress_completed
            if progress_successful is not None:
                job.progress.successful = progress_successful
            if progress_failed is not None:
                job.progress.failed = progress_failed
            if webhook_delivered is not None:
                job.webhook_delivered = webhook_delivered
            if webhook_error is not None:
                job.webhook_error = webhook_error
            self._job_path(job_id).write_text(job.model_dump_json(indent=2))
            return job

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _read_locked(self, job_id: str) -> Optional[Job]:
        """Read without acquiring lock — caller must hold it."""
        p = self._job_path(job_id)
        if not p.exists():
            return None
        try:
            return Job(**json.loads(p.read_text()))
        except Exception:
            return None

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._read_locked(job_id)

    def get_payload(self, job_id: str) -> Optional[Dict[str, Any]]:
        p = self._payload_path(job_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    def list_recent(
        self,
        limit: int = 50,
        *,
        status: Optional[JobStatus] = None,
        kind: Optional[JobKindFilter] = None,
    ) -> List[Job]:
        """
        Newest-first up to ``limit`` jobs that match optional ``status`` / ``kind``.
        """
        jobs: List[Job] = []
        with self._lock:
            paths = sorted(
                (
                    p
                    for p in self.jobs_dir.glob("*.json")
                    if not p.stem.endswith("_payload")
                ),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for p in paths:
                if len(jobs) >= limit:
                    break
                try:
                    job = Job(**json.loads(p.read_text()))
                except Exception:
                    continue
                if status is not None and job.status != status:
                    continue
                if kind is not None and job.kind != kind:
                    continue
                jobs.append(job)
        return jobs

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, job_id: str) -> bool:
        with self._lock:
            removed = False
            for p in [self._job_path(job_id), self._payload_path(job_id)]:
                if p.exists():
                    p.unlink()
                    removed = True
        return removed

    # ------------------------------------------------------------------
    # Helper: to summary
    # ------------------------------------------------------------------

    @staticmethod
    def to_summary(job: Job) -> JobSummary:
        return JobSummary(
            id=job.id,
            status=job.status,
            kind=job.kind,
            template_id=job.template_id,
            record_count=job.record_count,
            progress_pct=job.progress.pct,
            completed=job.progress.completed,
            successful=job.progress.successful,
            failed=job.progress.failed,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            download_url=job.download_url,
            avg_confidence=job.avg_confidence,
            cache_hits=job.cache_hits,
            error=job.error,
            webhook_url=job.webhook_url,
            webhook_delivered=job.webhook_delivered,
        )
