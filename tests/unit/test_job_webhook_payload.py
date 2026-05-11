"""Webhook payload shape for async jobs (integrations / Zapier-friendly)."""

import json
from unittest.mock import MagicMock, call, patch
from urllib.error import URLError

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


def test_fire_webhook_retries_then_succeeds(monkeypatch):
    from fillmypdf.config import settings

    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", None)
    monkeypatch.setattr(settings, "WEBHOOK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "WEBHOOK_RETRY_BASE_DELAY_SEC", 0.01)

    job = Job(
        id="job_retry_ok",
        status="done",
        kind="batch_fill",
        record_count=1,
        webhook_url="https://example.invalid/webhook",
        download_url="/api/v1/batch/download/x.zip",
        progress=JobProgress(total=1, completed=1, successful=1, failed=0),
    )
    repo = MagicMock()
    repo.get.return_value = job
    repo.get_payload.return_value = {}

    ok_cm = MagicMock()
    ok_cm.__enter__.return_value = MagicMock()
    ok_cm.__exit__.return_value = None

    outcomes = [URLError("boom1"), URLError("boom2"), ok_cm]

    def fake_open(*args, **kwargs):
        step = outcomes.pop(0)
        if isinstance(step, URLError):
            raise step
        return step

    with patch("fillmypdf.services.job_runner.time.sleep") as msleep:
        with patch(
            "fillmypdf.services.job_runner.urlopen", side_effect=fake_open
        ) as mu:
            JobRunner._fire_webhook("job_retry_ok", repo)

    assert mu.call_count == 3
    assert msleep.call_args_list == [call(0.01), call(0.02)]
    repo.update_status.assert_called_once_with(
        "job_retry_ok", status="done", webhook_delivered=True
    )


def test_fire_webhook_fails_after_max_attempts(monkeypatch):
    from fillmypdf.config import settings

    monkeypatch.setattr(settings, "WEBHOOK_SIGNING_SECRET", None)
    monkeypatch.setattr(settings, "WEBHOOK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "WEBHOOK_RETRY_BASE_DELAY_SEC", 0.01)

    job = Job(
        id="job_retry_fail",
        status="failed",
        kind="extract_pdf",
        record_count=1,
        webhook_url="https://example.invalid/webhook",
        download_url=None,
        progress=JobProgress(total=1, completed=0, successful=0, failed=1),
        error="x",
    )
    repo = MagicMock()
    repo.get.return_value = job
    repo.get_payload.return_value = {}

    err = URLError("final_reason")

    with patch("fillmypdf.services.job_runner.time.sleep") as msleep:
        with patch(
            "fillmypdf.services.job_runner.urlopen", side_effect=err
        ) as mu:
            JobRunner._fire_webhook("job_retry_fail", repo)

    assert mu.call_count == 3
    assert msleep.call_count == 2
    repo.update_status.assert_called_once()
    kw = repo.update_status.call_args.kwargs
    assert kw["webhook_delivered"] is False
    assert "final_reason" in kw["webhook_error"]
