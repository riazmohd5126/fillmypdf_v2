"""
Job Runner
===========
Executes batch fill jobs in a background thread pool.

Design:
  • One process-wide ThreadPoolExecutor (max_workers from config).
  • Jobs are submitted via ``submit()``, which enqueues work and returns the
    job ID immediately to the caller.
  • Progress is written back to ``JobRepository`` after every record for
    async batch / Excel / library-template fills so clients polling
    ``GET /api/v1/jobs/{id}`` see live counts.
  • Optionally HMAC-signed if ``WEBHOOK_SIGNING_SECRET`` or per-job ``webhook_secret`` is set.
  • The runner is started inside the FastAPI lifespan and shut down cleanly
    on process exit.
"""

from __future__ import annotations

import csv
import io
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from ..config import settings
from ..models.extract import PdfExtractResponse
from ..models.job import Job, JobProgress
from ..repositories.job_repository import JobRepository
from ..services.batch_fill_service import BatchFillService
from ..services.extraction_service import ExtractionService
from ..services.template_service import TemplateService
from ..services.webhook_signing import resolve_signing_secret, signature_headers


def _enqueue_webhook_redelivery_worker(job_id: str) -> None:
    """Runs in the executor — same retries/backoff as the automatic completion POST."""
    JobRunner._fire_webhook(job_id, JobRepository())


def _maybe_stash_webhook_secret(payload: Dict[str, Any], secret: Optional[str]) -> None:
    if secret and str(secret).strip():
        payload["webhook_secret"] = str(secret).strip()


class JobRunner:
    """
    Process-wide singleton (created once in lifespan).
    Call ``submit_batch()`` or ``submit_template_batch()`` from route handlers.
    """

    def __init__(self, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._repo = JobRepository()

    # ------------------------------------------------------------------
    # Public: submit jobs
    # ------------------------------------------------------------------

    def submit_batch(
        self,
        *,
        records: List[dict],
        template_pdf_bytes: bytes,
        template_filename: str,
        ai_api_key: str,
        ai_base_url: str,
        ai_model: str,
        dpi: int = 200,
        profile_id: Optional[str] = None,
        profile_ids: Optional[List[str]] = None,
        webhook_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        api_key_id: Optional[str] = None,
    ) -> Job:
        """Submit an ad-hoc batch fill (caller uploads the PDF)."""
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = Job(
            id=job_id,
            kind="batch_fill",
            api_key_id=api_key_id,
            record_count=len(records),
            webhook_url=webhook_url,
            progress=JobProgress(total=len(records)),
        )
        self._repo.save(job)

        # Persist PDF bytes + payload separately so the job record stays small
        pdf_path = settings.UPLOAD_DIR / f"{job_id}_template.pdf"
        pdf_path.write_bytes(template_pdf_bytes)

        payload: Dict[str, Any] = {
            "records": records,
            "template_pdf_path": str(pdf_path),
            "ai_api_key": ai_api_key,
            "ai_base_url": ai_base_url,
            "ai_model": ai_model,
            "dpi": dpi,
            "profile_id": profile_id,
            "profile_ids": profile_ids,
        }
        _maybe_stash_webhook_secret(payload, webhook_secret)
        self._repo.save_payload(job_id, payload)
        self._executor.submit(self._run_batch, job_id)
        return job

    def submit_xlsx_batch(
        self,
        *,
        template_pdf_bytes: bytes,
        xlsx_bytes: bytes,
        xlsx_filename: str,
        ai_api_key: str,
        ai_base_url: str,
        ai_model: str,
        dpi: int = 200,
        profile_id: Optional[str] = None,
        profile_ids: Optional[List[str]] = None,
        webhook_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        api_key_id: Optional[str] = None,
    ) -> Job:
        """Async batch fill: PDF template + Excel .xlsx (first sheet rows)."""
        batch_svc = BatchFillService()
        try:
            records = batch_svc.parse_xlsx(xlsx_bytes)
        except Exception as exc:
            raise ValueError(f"Invalid Excel workbook: {exc}") from exc
        if not records:
            raise ValueError("Excel file has no data rows")
        if len(records) > 500:
            raise ValueError("Maximum 500 rows per Excel job")

        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = Job(
            id=job_id,
            kind="batch_fill_xlsx",
            api_key_id=api_key_id,
            record_count=len(records),
            webhook_url=webhook_url,
            progress=JobProgress(total=len(records)),
        )
        self._repo.save(job)

        pdf_path = settings.UPLOAD_DIR / f"{job_id}_template.pdf"
        pdf_path.write_bytes(template_pdf_bytes)
        xlsx_path = settings.UPLOAD_DIR / f"{job_id}_data.xlsx"
        xlsx_path.write_bytes(xlsx_bytes)

        payload: Dict[str, Any] = {
            "template_pdf_path": str(pdf_path),
            "xlsx_path": str(xlsx_path),
            "xlsx_filename": xlsx_filename,
            "ai_api_key": ai_api_key,
            "ai_base_url": ai_base_url,
            "ai_model": ai_model,
            "dpi": dpi,
            "profile_id": profile_id,
            "profile_ids": profile_ids,
        }
        _maybe_stash_webhook_secret(payload, webhook_secret)
        self._repo.save_payload(job_id, payload)
        self._executor.submit(self._run_xlsx_batch, job_id)
        return job

    def submit_extract_pdf(
        self,
        *,
        pdf_bytes: bytes,
        source_filename: str,
        include_labels: bool = True,
        output_format: str = "json",
        webhook_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        api_key_id: Optional[str] = None,
    ) -> Job:
        """Queue extract (AcroForm → JSON or CSV artifact under OUTPUT_DIR)."""
        fmt = (output_format or "json").lower()
        if fmt not in ("json", "csv"):
            raise ValueError("output_format must be json or csv")

        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = Job(
            id=job_id,
            kind="extract_pdf",
            api_key_id=api_key_id,
            record_count=1,
            webhook_url=webhook_url,
            progress=JobProgress(total=1),
        )
        self._repo.save(job)

        pdf_path = settings.UPLOAD_DIR / f"{job_id}_extract_src.pdf"
        pdf_path.write_bytes(pdf_bytes)

        payload: Dict[str, Any] = {
            "pdf_path": str(pdf_path),
            "source_filename": source_filename,
            "include_labels": include_labels,
            "output_format": fmt,
        }
        _maybe_stash_webhook_secret(payload, webhook_secret)
        self._repo.save_payload(job_id, payload)
        self._executor.submit(self._run_extract_pdf, job_id)
        return job

    def submit_template_batch(
        self,
        *,
        template_id: str,
        records: List[dict],
        ai_api_key: str,
        ai_base_url: str,
        ai_model: str,
        dpi: int = 200,
        profile_id: Optional[str] = None,
        profile_ids: Optional[List[str]] = None,
        webhook_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        api_key_id: Optional[str] = None,
    ) -> Job:
        """Submit a batch fill against a stored library template."""
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = Job(
            id=job_id,
            kind="template_fill",
            template_id=template_id,
            api_key_id=api_key_id,
            record_count=len(records),
            webhook_url=webhook_url,
            progress=JobProgress(total=len(records)),
        )
        self._repo.save(job)

        payload: Dict[str, Any] = {
            "template_id": template_id,
            "records": records,
            "ai_api_key": ai_api_key,
            "ai_base_url": ai_base_url,
            "ai_model": ai_model,
            "dpi": dpi,
            "profile_id": profile_id,
            "profile_ids": profile_ids,
        }
        _maybe_stash_webhook_secret(payload, webhook_secret)
        self._repo.save_payload(job_id, payload)
        self._executor.submit(self._run_template_batch, job_id)
        return job

    def enqueue_webhook_redelivery(self, job_id: str) -> None:
        """Schedule another outbound completion webhook (non-blocking)."""
        self._executor.submit(_enqueue_webhook_redelivery_worker, job_id)

    # ------------------------------------------------------------------
    # Internal workers
    # ------------------------------------------------------------------

    def _run_batch(self, job_id: str) -> None:
        repo = JobRepository()
        payload = repo.get_payload(job_id)
        if payload is None:
            repo.update_status(job_id, status="failed", error="Payload missing")
            return

        repo.update_status(
            job_id, status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            batch_svc = BatchFillService()
            template_path = Path(payload["template_pdf_path"])

            def _on_tick(completed: int, successful: int, failed: int) -> None:
                repo.update_status(
                    job_id,
                    status="running",
                    progress_completed=completed,
                    progress_successful=successful,
                    progress_failed=failed,
                )

            result = batch_svc.process_batch_json(
                template_pdf_path=template_path,
                user_data_array=payload["records"],
                ai_api_key=payload["ai_api_key"],
                ai_base_url=payload["ai_base_url"],
                ai_model=payload["ai_model"],
                batch_id=job_id,
                dpi=payload.get("dpi", 200),
                profile_id=payload.get("profile_id"),
                profile_ids=payload.get("profile_ids"),
                on_record_done=_on_tick,
            )
            template_path.unlink(missing_ok=True)

            results = result.get("results", [])
            successful = result.get("successful", 0)
            failed_count = result.get("failed", 0)
            conf_vals = [r["avg_confidence"] for r in results if r.get("avg_confidence") is not None]
            avg_conf = round(sum(conf_vals) / len(conf_vals), 3) if conf_vals else None
            cache_hits = sum(1 for r in results if r.get("cache_hit"))

            repo.update_status(
                job_id,
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                download_url=result.get("download_url"),
                avg_confidence=avg_conf,
                cache_hits=cache_hits,
                progress_completed=len(results),
                progress_successful=successful,
                progress_failed=failed_count,
            )
        except Exception as exc:
            repo.update_status(
                job_id,
                status="failed",
                error=str(exc),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            self._fire_webhook(job_id, repo)

    def _run_xlsx_batch(self, job_id: str) -> None:
        repo = JobRepository()
        payload = repo.get_payload(job_id)
        if payload is None:
            repo.update_status(job_id, status="failed", error="Payload missing")
            return

        repo.update_status(
            job_id,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        template_path = Path(payload["template_pdf_path"])
        xlsx_path = Path(payload["xlsx_path"])
        try:
            batch_svc = BatchFillService()
            xlsx_content = xlsx_path.read_bytes()

            def _on_tick(completed: int, successful: int, failed: int) -> None:
                repo.update_status(
                    job_id,
                    status="running",
                    progress_completed=completed,
                    progress_successful=successful,
                    progress_failed=failed,
                )

            result = batch_svc.process_xlsx_batch(
                template_pdf_path=template_path,
                xlsx_content=xlsx_content,
                xlsx_filename=payload.get("xlsx_filename", "rows.xlsx"),
                ai_api_key=payload["ai_api_key"],
                ai_base_url=payload["ai_base_url"],
                ai_model=payload["ai_model"],
                batch_id=job_id,
                dpi=payload.get("dpi", 200),
                profile_id=payload.get("profile_id"),
                profile_ids=payload.get("profile_ids"),
                on_record_done=_on_tick,
            )

            template_path.unlink(missing_ok=True)
            xlsx_path.unlink(missing_ok=True)

            results = result.get("results", [])
            successful = result.get("successful", 0)
            failed_count = result.get("failed", 0)
            conf_vals = [
                r["avg_confidence"] for r in results if r.get("avg_confidence") is not None
            ]
            avg_conf = round(sum(conf_vals) / len(conf_vals), 3) if conf_vals else None
            cache_hits = sum(1 for r in results if r.get("cache_hit"))

            repo.update_status(
                job_id,
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                download_url=result.get("download_url"),
                avg_confidence=avg_conf,
                cache_hits=cache_hits,
                progress_completed=len(results),
                progress_successful=successful,
                progress_failed=failed_count,
            )
        except Exception as exc:
            template_path.unlink(missing_ok=True)
            xlsx_path.unlink(missing_ok=True)
            repo.update_status(
                job_id,
                status="failed",
                error=str(exc),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            self._fire_webhook(job_id, repo)

    @staticmethod
    def _extract_result_to_csv_bytes(result: PdfExtractResponse) -> bytes:
        buf = io.StringIO()
        w = csv.DictWriter(
            buf,
            fieldnames=["name", "label", "value", "page", "field_type"],
            extrasaction="ignore",
        )
        w.writeheader()
        for row in result.fields:
            w.writerow(row.model_dump())
        return ("\ufeff" + buf.getvalue()).encode("utf-8")

    def _run_extract_pdf(self, job_id: str) -> None:
        repo = JobRepository()
        payload = repo.get_payload(job_id)
        if payload is None:
            repo.update_status(job_id, status="failed", error="Payload missing")
            return

        pdf_path = Path(payload["pdf_path"])
        repo.update_status(
            job_id,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            svc = ExtractionService()
            result = svc.extract_pdf(
                pdf_path, include_labels=bool(payload.get("include_labels", True))
            )
            result = result.model_copy(update={"filename": payload.get("source_filename")})
            fmt = str(payload.get("output_format") or "json").lower()

            if fmt == "csv":
                out_name = f"{job_id}_extract.csv"
                body = JobRunner._extract_result_to_csv_bytes(result)
                (settings.OUTPUT_DIR / out_name).write_bytes(body)
            else:
                out_name = f"{job_id}_extract.json"
                (settings.OUTPUT_DIR / out_name).write_text(
                    result.model_dump_json(indent=2), encoding="utf-8"
                )

            pdf_path.unlink(missing_ok=True)

            repo.update_status(
                job_id,
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                download_url=f"/api/v1/batch/download/{out_name}",
                progress_completed=1,
                progress_successful=1,
                progress_failed=0,
            )
        except Exception as exc:
            pdf_path.unlink(missing_ok=True)
            repo.update_status(
                job_id,
                status="failed",
                error=str(exc),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            self._fire_webhook(job_id, repo)

    def _run_template_batch(self, job_id: str) -> None:
        repo = JobRepository()
        payload = repo.get_payload(job_id)
        if payload is None:
            repo.update_status(job_id, status="failed", error="Payload missing")
            return

        repo.update_status(
            job_id, status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            tmpl_svc = TemplateService()

            def _on_tick(completed: int, successful: int, failed: int) -> None:
                repo.update_status(
                    job_id,
                    status="running",
                    progress_completed=completed,
                    progress_successful=successful,
                    progress_failed=failed,
                )

            resp = tmpl_svc.fill_batch(
                template_id=payload["template_id"],
                records=payload["records"],
                ai_api_key=payload["ai_api_key"],
                ai_base_url=payload["ai_base_url"],
                ai_model=payload["ai_model"],
                dpi=payload.get("dpi", 200),
                profile_id=payload.get("profile_id"),
                profile_ids=payload.get("profile_ids"),
                on_record_done=_on_tick,
            )
            repo.update_status(
                job_id,
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                download_url=resp.download_url,
                avg_confidence=resp.avg_confidence,
                cache_hits=resp.cache_hits,
                progress_completed=resp.total_records,
                progress_successful=resp.successful,
                progress_failed=resp.failed,
            )
        except Exception as exc:
            repo.update_status(
                job_id,
                status="failed",
                error=str(exc),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            self._fire_webhook(job_id, repo)

    # ------------------------------------------------------------------
    # Webhook delivery
    # ------------------------------------------------------------------

    @staticmethod
    def _fire_webhook(job_id: str, repo: JobRepository) -> None:
        """POST a JSON snapshot to webhook_url — fields align with GET /jobs/{id}.

        Uses ``WEBHOOK_MAX_ATTEMPTS`` and exponential ``WEBHOOK_RETRY_BASE_DELAY_SEC``
        backoff on network/HTTP errors (``URLError`` / ``HTTPError``, ``OSError``).
        """
        job = repo.get(job_id)
        if job is None or not job.webhook_url:
            return

        payload = repo.get_payload(job_id) or {}

        body_bytes = json.dumps(
            {
                "event": "job.completed",
                "job_id": job.id,
                "kind": job.kind,
                "status": job.status,
                "template_id": job.template_id,
                "record_count": job.record_count,
                "completed": job.progress.completed,
                "total": job.progress.total,
                "successful": job.progress.successful,
                "failed": job.progress.failed,
                "progress_pct": job.progress.pct,
                "download_url": job.download_url,
                "avg_confidence": job.avg_confidence,
                "cache_hits": job.cache_hits,
                "error": job.error,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
            },
            default=str,
        ).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "X-FillMyPDF-Event": "job.completed",
            "X-FillMyPDF-Job-ID": job.id,
            "X-FillMyPDF-Job-Kind": job.kind,
        }

        signing_secret = resolve_signing_secret(
            payload_webhook_secret=payload.get("webhook_secret")
        )
        if signing_secret:
            headers.update(signature_headers(signing_secret, body_bytes))

        req = Request(
            url=job.webhook_url,
            data=body_bytes,
            headers=headers,
            method="POST",
        )
        attempts = max(1, int(getattr(settings, "WEBHOOK_MAX_ATTEMPTS", 4)))
        base_delay = float(getattr(settings, "WEBHOOK_RETRY_BASE_DELAY_SEC", 1.0))
        last_exc: Optional[BaseException] = None

        for attempt in range(attempts):
            try:
                with urlopen(req, timeout=10):
                    pass
                repo.update_status(job_id, status=job.status, webhook_delivered=True)
                return
            except (URLError, OSError) as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    break
                delay = base_delay * (2**attempt)
                time.sleep(delay)

        repo.update_status(
            job_id,
            status=job.status,
            webhook_delivered=False,
            webhook_error=str(last_exc) if last_exc else "webhook delivery failed",
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)


# ---------------------------------------------------------------------------
# Process-wide singleton — initialised in lifespan
# ---------------------------------------------------------------------------
_runner: Optional[JobRunner] = None


def get_runner() -> JobRunner:
    global _runner
    if _runner is None:
        _runner = JobRunner(max_workers=settings.JOB_WORKER_THREADS)
    return _runner


def shutdown_runner() -> None:
    global _runner
    if _runner is not None:
        _runner.shutdown(wait=False)
        _runner = None
