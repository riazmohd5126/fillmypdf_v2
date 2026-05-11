"""Webhook payload shape for async jobs (integrations / Zapier-friendly)."""

import json
from unittest.mock import MagicMock, patch

from fillmypdf.models.job import Job, JobProgress
from fillmypdf.services.job_runner import JobRunner
from fillmypdf.services.webhook_signing import verify


def test_fire_webhook_includes_flat_fields_and_kind_header(monkeypatch):
    from fillmypdf.config import settings

    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", None)

    job = Job(
        id="job_webhook_demo",
        status="done",
        kind="extract_pdf",
        record_count=1,
        webhook_url="https://example.invalid/webhook",
        download_url="/api/v1/batch/download/job_webhook_demo_extract.json",
        avg_confidence=None,
        progress=JobProgress(total=1, completed=1, successful=1, failed=0),
    )
    repo = MagicMock()
    repo.get.return_value = job
    repo.get_payload.return_value = {}

    cm_enter = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = cm_enter
    cm.__exit__.return_value = None

    with patch("fillmypdf.services.job_runner.urlopen") as mock_urlopen:
        mock_urlopen.return_value = cm
        JobRunner._fire_webhook("job_webhook_demo", repo)

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    body = json.loads(req.data.decode())

    assert body["event"] == "job.completed"
    assert body["job_id"] == "job_webhook_demo"
    assert body["kind"] == "extract_pdf"
    assert body["status"] == "done"
    assert body["total"] == body["completed"] == 1
    assert body["progress_pct"] == 100.0
    assert body["successful"] == 1 and body["failed"] == 0
    assert "created_at" in body and body["download_url"]

    hdr = {k.lower(): v for k, v in req.header_items()}
    assert hdr.get("x-fillmypdf-job-kind") == "extract_pdf"
    assert "x-fillmypdf-signature" not in hdr

    repo.update_status.assert_called_with(
        "job_webhook_demo", status="done", webhook_delivered=True
    )


def test_fire_webhook_hmac_headers_verify():
    job = Job(
        id="job_hmac",
        status="done",
        kind="batch_fill",
        record_count=2,
        webhook_url="https://example.invalid/webhook",
        download_url="/api/v1/batch/download/x.zip",
        progress=JobProgress(total=2, completed=2, successful=2, failed=0),
    )
    repo = MagicMock()
    repo.get.return_value = job
    repo.get_payload.return_value = {"webhook_secret": "unit-test-secret"}

    cm = MagicMock()
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = None

    with patch(
        "fillmypdf.services.webhook_signing.time.time",
        return_value=1_719_880_011.42,
    ):
        with patch("fillmypdf.services.job_runner.urlopen") as mock_urlopen:
            mock_urlopen.return_value = cm
            JobRunner._fire_webhook("job_hmac", repo)

    req = mock_urlopen.call_args[0][0]
    body_raw = req.data
    hdr = {k.lower(): v for k, v in req.header_items()}

    assert hdr["x-fillmypdf-signature"].startswith("v1=")
    assert verify(
        secret="unit-test-secret",
        timestamp_header=hdr["x-fillmypdf-timestamp"],
        body=body_raw,
        signature_header=hdr["x-fillmypdf-signature"],
        max_age_seconds=10**9,
    )
