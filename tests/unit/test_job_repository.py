"""
Unit tests — JobRepository
===========================
"""
import json
import pytest
from fillmypdf.models.job import Job, JobProgress
from fillmypdf.repositories.job_repository import JobRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "fillmypdf.repositories.job_repository.settings",
        type("S", (), {"STORAGE_DIR": tmp_path})(),
    )
    return JobRepository()


def _job(jid="job_test123") -> Job:
    return Job(
        id=jid,
        kind="batch_fill",
        record_count=10,
        progress=JobProgress(total=10),
    )


class TestSaveAndGet:
    def test_save_creates_file(self, repo, tmp_path):
        repo.save(_job())
        assert (tmp_path / "jobs" / "job_test123.json").exists()

    def test_get_returns_job(self, repo):
        repo.save(_job())
        j = repo.get("job_test123")
        assert j is not None
        assert j.id == "job_test123"
        assert j.status == "queued"

    def test_get_missing_returns_none(self, repo):
        assert repo.get("ghost") is None


class TestPayload:
    def test_save_and_get_payload(self, repo):
        repo.save(_job())
        repo.save_payload("job_test123", {"records": [{"a": 1}]})
        p = repo.get_payload("job_test123")
        assert p is not None
        assert p["records"][0]["a"] == 1

    def test_get_missing_payload_returns_none(self, repo):
        assert repo.get_payload("ghost") is None


class TestUpdateStatus:
    def test_update_to_running(self, repo):
        repo.save(_job())
        repo.update_status("job_test123", status="running", started_at="2026-01-01T00:00:00+00:00")
        j = repo.get("job_test123")
        assert j.status == "running"
        assert j.started_at == "2026-01-01T00:00:00+00:00"

    def test_update_progress(self, repo):
        repo.save(_job())
        repo.update_status("job_test123", status="running",
                           progress_completed=5, progress_successful=4, progress_failed=1)
        j = repo.get("job_test123")
        assert j.progress.completed == 5
        assert j.progress.successful == 4

    def test_update_done_with_url(self, repo):
        repo.save(_job())
        repo.update_status("job_test123", status="done",
                           download_url="/api/v1/jobs/job_test123/download",
                           avg_confidence=0.92, cache_hits=3)
        j = repo.get("job_test123")
        assert j.status == "done"
        assert j.avg_confidence == 0.92
        assert j.cache_hits == 3

    def test_cache_hits_can_be_zero(self, repo):
        repo.save(_job())
        repo.update_status(
            "job_test123",
            status="done",
            cache_hits=0,
        )
        assert repo.get("job_test123").cache_hits == 0

    def test_update_missing_returns_none(self, repo):
        result = repo.update_status("ghost", status="done")
        assert result is None

    def test_update_webhook_delivered(self, repo):
        repo.save(_job())
        repo.update_status("job_test123", status="done", webhook_delivered=True)
        j = repo.get("job_test123")
        assert j.webhook_delivered is True


class TestList:
    def test_list_returns_all(self, repo):
        repo.save(_job("job_a"))
        repo.save(_job("job_b"))
        jobs = repo.list_recent(limit=10)
        assert len(jobs) == 2

    def test_list_respects_limit(self, repo):
        for i in range(5):
            repo.save(_job(f"job_{i}"))
        jobs = repo.list_recent(limit=3)
        assert len(jobs) == 3

    def test_list_filter_status(self, repo):
        repo.save(Job(id="d", record_count=0, status="done"))
        repo.save(Job(id="r", record_count=0, status="running"))
        out = repo.list_recent(10, status="done")
        assert len(out) == 1 and out[0].id == "d"

    def test_list_filter_kind(self, repo):
        repo.save(Job(id="x", record_count=0, kind="extract_pdf"))
        repo.save(Job(id="y", record_count=0, kind="batch_fill"))
        out = repo.list_recent(10, kind="extract_pdf")
        assert len(out) == 1 and out[0].id == "x"

    def test_filter_combined_fills_from_newer_first(self, repo):
        for i in range(4):
            repo.save(Job(id=f"q{i}", record_count=0, status="queued"))
        repo.save(Job(id="d0", record_count=0, status="done"))
        repo.save(Job(id="d1", record_count=0, status="done"))
        out = repo.list_recent(2, status="done")
        assert [j.id for j in out] == ["d1", "d0"]

    def test_list_empty(self, repo):
        assert repo.list_recent() == []


class TestDelete:
    def test_delete_removes_job(self, repo):
        repo.save(_job())
        repo.save_payload("job_test123", {"x": 1})
        assert repo.delete("job_test123") is True
        assert repo.get("job_test123") is None
        assert repo.get_payload("job_test123") is None

    def test_delete_missing_returns_false(self, repo):
        assert repo.delete("ghost") is False


class TestToSummary:
    def test_summary_fields(self, repo):
        j = _job()
        j.progress.total = 10
        j.progress.completed = 7
        j.progress.successful = 6
        j.progress.failed = 1
        s = repo.to_summary(j)
        assert s.id == "job_test123"
        assert s.progress_pct == 70.0
        assert s.successful == 6

    def test_progress_pct_zero_total(self, repo):
        j = Job(id="x", record_count=0, progress=JobProgress(total=0))
        s = repo.to_summary(j)
        assert s.progress_pct == 0.0
