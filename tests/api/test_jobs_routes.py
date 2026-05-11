"""
API integration tests — /api/v1/jobs
======================================
The actual job runner (thread pool, VisionService, BatchFillService) is fully
mocked so tests run instantly with no AI keys or PDFs.
"""
import json
import pytest
from unittest.mock import MagicMock, patch
from fillmypdf.models.job import Job, JobProgress, JobSubmitResponse, JobSummary

BASE = "/api/v1/jobs"


def _plain(k): return k["plain"]


def _queued_job(jid="job_abc123", kind="batch_fill"):
    return Job(
        id=jid,
        status="queued",
        kind=kind,
        record_count=3,
        progress=JobProgress(total=3),
    )


def _done_job(jid="job_abc123"):
    j = _queued_job(jid)
    j.status = "done"
    j.progress.completed = 3
    j.progress.successful = 3
    j.download_url = f"/api/v1/batch/download/{jid}.zip"
    return j


def _summary(job: Job):
    from fillmypdf.repositories.job_repository import JobRepository
    return JobRepository.to_summary(job)


# ---------------------------------------------------------------------------
# Submit batch job (upload PDF)
# ---------------------------------------------------------------------------


class TestSubmitBatchJob:

    def test_submit_returns_202(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
            mock_runner.return_value.submit_batch.return_value = _queued_job()
            resp = client.post(
                f"{BASE}/batch",
                data={
                    "ai_api_key": "test",
                    "records": json.dumps([{"first_name": "Alice"}]),
                },
                files={"file": ("pa.pdf", b"%PDF-1.4", "application/pdf")},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] == "job_abc123"
        assert body["status"] == "queued"
        assert "/api/v1/jobs/" in body["status_url"]

    def test_submit_non_pdf_returns_400(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs.get_runner"):
            resp = client.post(
                f"{BASE}/batch",
                data={"ai_api_key": "k", "records": "[{}]"},
                files={"file": ("f.docx", b"word", "application/msword")},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400

    def test_submit_empty_records_returns_400(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs.get_runner"):
            resp = client.post(
                f"{BASE}/batch",
                data={"ai_api_key": "k", "records": "[]"},
                files={"file": ("pa.pdf", b"%PDF", "application/pdf")},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400

    def test_submit_too_many_records_returns_400(self, client, pro_api_key):
        big = json.dumps([{"x": i} for i in range(501)])
        with patch("fillmypdf.api.routes.jobs.get_runner"):
            resp = client.post(
                f"{BASE}/batch",
                data={"ai_api_key": "k", "records": big},
                files={"file": ("pa.pdf", b"%PDF", "application/pdf")},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400

    def test_submit_requires_auth(self, client):
        resp = client.post(f"{BASE}/batch", data={}, files={})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Submit template-batch job
# ---------------------------------------------------------------------------


class TestSubmitTemplateBatchJob:

    def test_submit_template_returns_202(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
            mock_runner.return_value.submit_template_batch.return_value = _queued_job(
                kind="template_fill"
            )
            resp = client.post(
                f"{BASE}/template-batch",
                data={
                    "template_id": "pa_linzess_molina_tx",
                    "ai_api_key": "test",
                    "records": json.dumps([{"first_name": "Bob"}]),
                },
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"

    def test_submit_template_empty_returns_400(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs.get_runner"):
            resp = client.post(
                f"{BASE}/template-batch",
                data={"template_id": "pa_x", "ai_api_key": "k", "records": "[]"},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Submit xlsx-batch job
# ---------------------------------------------------------------------------


class TestSubmitXlsxJob:

    def test_submit_returns_202(self, client, pro_api_key):
        j = _queued_job(kind="batch_fill_xlsx")
        j.record_count = 4
        with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
            mock_runner.return_value.submit_xlsx_batch.return_value = j
            resp = client.post(
                f"{BASE}/xlsx-batch",
                data={"ai_api_key": "test"},
                files={
                    "file": ("pa.pdf", b"%PDF-1.4", "application/pdf"),
                    "xlsx_file": ("rows.xlsx", b"PK fake xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                },
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] == "job_abc123"
        assert "Excel job queued" in body["message"]

    def test_non_xlsx_returns_400(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs.get_runner"):
            resp = client.post(
                f"{BASE}/xlsx-batch",
                data={"ai_api_key": "k"},
                files={
                    "file": ("pa.pdf", b"%PDF", "application/pdf"),
                    "xlsx_file": ("bad.csv", b"a,b", "text/csv"),
                },
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400

    def test_parse_error_returns_400(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
            mock_runner.return_value.submit_xlsx_batch.side_effect = ValueError("Invalid Excel")
            resp = client.post(
                f"{BASE}/xlsx-batch",
                data={"ai_api_key": "k"},
                files={
                    "file": ("pa.pdf", b"%PDF", "application/pdf"),
                    "xlsx_file": ("rows.xlsx", b"x", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                },
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400
        assert "Invalid Excel" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Submit extract job (AcroForm → JSON/CSV artifact)
# ---------------------------------------------------------------------------


class TestSubmitExtractJob:

    def test_submit_returns_202_json(self, client, pro_api_key):
        j = _queued_job(kind="extract_pdf")
        j.record_count = 1
        j.progress.total = 1
        with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
            mock_runner.return_value.submit_extract_pdf.return_value = j
            resp = client.post(
                f"{BASE}/extract",
                data={
                    "include_labels": "true",
                    "output_format": "json",
                },
                files={"file": ("filled.pdf", b"%PDF-1.4", "application/pdf")},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] == "job_abc123"
        assert "Extract job queued (JSON)" in body["message"]

    def test_submit_csv_message(self, client, pro_api_key):
        j = _queued_job(kind="extract_pdf")
        with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
            mock_runner.return_value.submit_extract_pdf.return_value = j
            resp = client.post(
                f"{BASE}/extract",
                data={
                    "include_labels": "false",
                    "output_format": "csv",
                },
                files={"file": ("filled.pdf", b"%PDF", "application/pdf")},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 202
        assert "CSV)" in resp.json()["message"]

    def test_non_pdf_returns_400(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs.get_runner"):
            resp = client.post(
                f"{BASE}/extract",
                data={"output_format": "json"},
                files={"file": ("readme.txt", b"hi", "text/plain")},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400

    def test_submit_raises_value_returns_400(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
            mock_runner.return_value.submit_extract_pdf.side_effect = ValueError("bad fmt")
            resp = client.post(
                f"{BASE}/extract",
                files={"file": ("filled.pdf", b"%PDF", "application/pdf")},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400
        assert "bad fmt" in resp.json()["detail"]

    def test_requires_auth(self, client):
        resp = client.post(f"{BASE}/extract", files={})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Get job status
# ---------------------------------------------------------------------------


class TestGetJob:

    def test_get_queued_job(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = _queued_job()
            mock_repo.return_value.to_summary = JobSummary.model_validate
            from fillmypdf.repositories.job_repository import JobRepository
            mock_repo.return_value.to_summary = JobRepository.to_summary
            resp = client.get(
                f"{BASE}/job_abc123",
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "job_abc123"
        assert body["status"] == "queued"

    def test_get_missing_returns_404(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = None
            resp = client.get(
                f"{BASE}/ghost",
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 404

    def test_get_requires_auth(self, client):
        resp = client.get(f"{BASE}/job_abc123")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Retry webhook
# ---------------------------------------------------------------------------


class TestRetryWebhook:

    def test_retry_accepted_terminal_job(self, client, pro_api_key):
        done = _done_job()
        done.webhook_url = "https://hooks.example.invalid/cb"

        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = done
            with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
                resp = client.post(
                    f"{BASE}/job_abc123/retry-webhook",
                    headers={"X-API-Key": _plain(pro_api_key)},
                )
        assert resp.status_code == 202
        assert resp.json()["job_id"] == "job_abc123"
        mock_runner.return_value.enqueue_webhook_redelivery.assert_called_once_with(
            "job_abc123"
        )

    def test_retry_409_while_running(self, client, pro_api_key):
        running = _queued_job()
        running.status = "running"
        running.webhook_url = "https://hooks.example.invalid/cb"

        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = running
            with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
                resp = client.post(
                    f"{BASE}/job_abc123/retry-webhook",
                    headers={"X-API-Key": _plain(pro_api_key)},
                )
        assert resp.status_code == 409
        mock_runner.return_value.enqueue_webhook_redelivery.assert_not_called()

    def test_retry_400_no_webhook_url(self, client, pro_api_key):
        done = _done_job()
        done.webhook_url = None

        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = done
            with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
                resp = client.post(
                    f"{BASE}/job_abc123/retry-webhook",
                    headers={"X-API-Key": _plain(pro_api_key)},
                )
        assert resp.status_code == 400
        mock_runner.return_value.enqueue_webhook_redelivery.assert_not_called()

    def test_retry_404_missing_job(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = None
            with patch("fillmypdf.api.routes.jobs.get_runner") as mock_runner:
                resp = client.post(
                    f"{BASE}/ghost/retry-webhook",
                    headers={"X-API-Key": _plain(pro_api_key)},
                )
        assert resp.status_code == 404
        mock_runner.return_value.enqueue_webhook_redelivery.assert_not_called()

    def test_retry_requires_auth(self, client):
        resp = client.post(f"{BASE}/job_x/retry-webhook")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Download redirect
# ---------------------------------------------------------------------------


class TestDownloadJobResult:

    def test_download_done_job(self, client, pro_api_key, tmp_path, monkeypatch):
        # Create a fake ZIP in OUTPUT_DIR
        job = _done_job()
        zip_name = "job_abc123.zip"
        job.download_url = f"/api/v1/batch/download/{zip_name}"

        import fillmypdf.config as cfg
        fake_out = tmp_path / "outputs"
        fake_out.mkdir()
        monkeypatch.setattr(cfg.settings, "OUTPUT_DIR", fake_out)
        (fake_out / zip_name).write_bytes(b"PK fake zip")

        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo, \
             patch("fillmypdf.api.routes.jobs.settings", OUTPUT_DIR=fake_out):
            mock_repo.return_value.get.return_value = job
            resp = client.get(
                f"{BASE}/job_abc123/download",
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        # File exists → 200 (FileResponse)
        assert resp.status_code == 200
        assert "zip" in resp.headers.get("content-type", "").lower()

    def test_download_done_extract_json(self, client, pro_api_key, tmp_path, monkeypatch):
        job = _done_job()
        jname = "job_abc123_extract.json"
        job.download_url = f"/api/v1/batch/download/{jname}"

        import fillmypdf.config as cfg
        fake_out = tmp_path / "outputs"
        fake_out.mkdir()
        monkeypatch.setattr(cfg.settings, "OUTPUT_DIR", fake_out)
        (fake_out / jname).write_text('{"success":true}', encoding="utf-8")

        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo, \
             patch("fillmypdf.api.routes.jobs.settings", OUTPUT_DIR=fake_out):
            mock_repo.return_value.get.return_value = job
            resp = client.get(
                f"{BASE}/job_abc123/download",
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 200
        assert "json" in resp.headers.get("content-type", "").lower()

    def test_download_running_job_returns_409(self, client, pro_api_key):
        running = _queued_job()
        running.status = "running"
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = running
            resp = client.get(
                f"{BASE}/job_abc123/download",
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 409

    def test_download_failed_job_returns_410(self, client, pro_api_key):
        failed = _queued_job()
        failed.status = "failed"
        failed.error = "conversion failed"
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = failed
            resp = client.get(
                f"{BASE}/job_abc123/download",
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 410


# ---------------------------------------------------------------------------
# List jobs
# ---------------------------------------------------------------------------


class TestListJobs:

    def test_list_returns_jobs(self, client, pro_api_key):
        jobs = [_queued_job("job_1"), _queued_job("job_2")]
        from fillmypdf.repositories.job_repository import JobRepository
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.list_recent.return_value = jobs
            mock_repo.return_value.to_summary = JobRepository.to_summary
            resp = client.get(BASE, headers={"X-API-Key": _plain(pro_api_key)})
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_list_empty(self, client, pro_api_key):
        from fillmypdf.repositories.job_repository import JobRepository
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.list_recent.return_value = []
            mock_repo.return_value.to_summary = JobRepository.to_summary
            resp = client.get(BASE, headers={"X-API-Key": _plain(pro_api_key)})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_list_passes_query_filters(self, client, pro_api_key):
        from fillmypdf.repositories.job_repository import JobRepository
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            inst = mock_repo.return_value
            inst.list_recent.return_value = []
            inst.to_summary = JobRepository.to_summary
            resp = client.get(
                f"{BASE}?status=running&kind=extract_pdf&limit=17",
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 200
        inst.list_recent.assert_called_once_with(
            limit=17, status="running", kind="extract_pdf"
        )

    def test_list_requires_auth(self, client):
        resp = client.get(BASE)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Cancel / delete
# ---------------------------------------------------------------------------


class TestCancelJob:

    def test_cancel_queued_returns_204(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = _queued_job()
            mock_repo.return_value.update_status.return_value = None
            resp = client.delete(
                f"{BASE}/job_abc123",
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 204

    def test_cancel_done_deletes_record(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = _done_job()
            mock_repo.return_value.delete.return_value = True
            resp = client.delete(
                f"{BASE}/job_abc123",
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 204

    def test_cancel_missing_returns_404(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.jobs._repo") as mock_repo:
            mock_repo.return_value.get.return_value = None
            resp = client.delete(
                f"{BASE}/ghost",
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 404

    def test_cancel_requires_auth(self, client):
        resp = client.delete(f"{BASE}/job_abc123")
        assert resp.status_code == 401
