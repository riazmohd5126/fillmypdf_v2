"""
Unit tests for ApprovalService
================================
Tests run purely against the service with a temp directory — no network, no
FastAPI, no webhooks.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from fillmypdf.services.approval_service import ApprovalRequest, ApprovalService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(tmp_path):
    """Return an ApprovalService whose STORAGE_DIR is in tmp_path."""
    with patch("fillmypdf.services.approval_service.settings") as mock_settings:
        mock_settings.STORAGE_DIR = tmp_path
        svc = ApprovalService()
        # Patch the property on the instance so it keeps pointing to tmp_path
        with patch.object(
            type(svc), "_dir", new_callable=property, fget=lambda self: tmp_path / "approvals"
        ):
            yield svc


@pytest.fixture()
def svc(tmp_path):
    """ApprovalService backed by a tmp directory."""
    with patch("fillmypdf.services.approval_service.settings") as mock_cfg:
        mock_cfg.STORAGE_DIR = tmp_path
        service = ApprovalService()
        # Redirect internal _dir property to tmp_path/approvals
        original_dir_getter = ApprovalService._dir.fget

        def patched_dir(self):
            p = tmp_path / "approvals"
            p.mkdir(parents=True, exist_ok=True)
            return p

        with patch.object(ApprovalService, "_dir", new_callable=property, fget=patched_dir):
            yield service


def _create(svc: ApprovalService, **kwargs) -> ApprovalRequest:
    defaults = dict(
        title="Test Doc",
        pdf_filename="test.pdf",
        reviewer_email="reviewer@example.com",
    )
    defaults.update(kwargs)
    return svc.create(**defaults)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_create_returns_pending_record(self, svc):
        rec = _create(svc)
        assert rec.status == "pending"
        assert rec.title == "Test Doc"
        assert rec.pdf_filename == "test.pdf"
        assert rec.approval_id.startswith("appr_")
        assert len(rec.review_token) > 20

    def test_get_returns_created_record(self, svc):
        rec = _create(svc)
        fetched = svc.get(rec.approval_id)
        assert fetched is not None
        assert fetched.approval_id == rec.approval_id
        assert fetched.status == "pending"

    def test_decide_approve(self, svc):
        rec = _create(svc)
        updated = svc.decide(
            rec.approval_id,
            decision="approved",
            comment="Looks good.",
            token=rec.review_token,
        )
        assert updated.status == "approved"
        assert updated.comment == "Looks good."
        assert updated.decided_at is not None

    def test_decide_reject(self, svc):
        rec = _create(svc)
        updated = svc.decide(
            rec.approval_id,
            decision="rejected",
            comment="Missing info.",
            token=rec.review_token,
        )
        assert updated.status == "rejected"
        assert updated.comment == "Missing info."

    def test_decide_persists_to_disk(self, svc):
        rec = _create(svc)
        svc.decide(rec.approval_id, decision="approved", comment=None, token=rec.review_token)
        # Re-fetch from disk
        reloaded = svc.get(rec.approval_id)
        assert reloaded is not None
        assert reloaded.status == "approved"

    def test_verify_token_for_read_valid(self, svc):
        rec = _create(svc)
        result = svc.verify_token_for_read(rec.approval_id, rec.review_token)
        assert result.approval_id == rec.approval_id

    def test_list_all_includes_created(self, svc):
        ids = {_create(svc, title=f"Doc {i}", pdf_filename=f"f{i}.pdf").approval_id for i in range(3)}
        listed = {r.approval_id for r in svc.list_all()}
        assert ids.issubset(listed)

    def test_decide_with_empty_comment_stores_none(self, svc):
        rec = _create(svc)
        updated = svc.decide(
            rec.approval_id, decision="approved", comment="   ", token=rec.review_token
        )
        assert updated.comment is None


# ---------------------------------------------------------------------------
# Bad token
# ---------------------------------------------------------------------------

class TestBadToken:
    def test_decide_wrong_token_raises_permission_error(self, svc):
        rec = _create(svc)
        with pytest.raises(PermissionError, match="Invalid review token"):
            svc.decide(rec.approval_id, decision="approved", comment=None, token="wrong-token")

    def test_verify_token_for_read_wrong_token_raises(self, svc):
        rec = _create(svc)
        with pytest.raises(PermissionError):
            svc.verify_token_for_read(rec.approval_id, "totally-wrong")

    def test_decide_empty_token_raises_permission_error(self, svc):
        rec = _create(svc)
        with pytest.raises(PermissionError):
            svc.decide(rec.approval_id, decision="approved", comment=None, token="")


# ---------------------------------------------------------------------------
# Double-decide guard
# ---------------------------------------------------------------------------

class TestDoubleDecide:
    def test_second_decide_raises_value_error(self, svc):
        rec = _create(svc)
        svc.decide(rec.approval_id, decision="approved", comment=None, token=rec.review_token)
        with pytest.raises(ValueError, match="already decided"):
            svc.decide(rec.approval_id, decision="rejected", comment=None, token=rec.review_token)

    def test_second_decide_different_decision_still_blocked(self, svc):
        rec = _create(svc)
        svc.decide(rec.approval_id, decision="rejected", comment=None, token=rec.review_token)
        with pytest.raises(ValueError):
            svc.decide(rec.approval_id, decision="rejected", comment=None, token=rec.review_token)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

class TestExpiry:
    def test_is_expired_false_for_fresh_record(self, svc):
        rec = _create(svc, expires_in_hours=168)
        assert not rec.is_expired()

    def test_is_expired_true_when_past(self, svc):
        rec = _create(svc, expires_in_hours=1)
        # Monkeypatch expires_at to a past timestamp
        rec._d["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        assert rec.is_expired()

    def test_verify_token_for_read_raises_on_expired(self, svc):
        rec = _create(svc, expires_in_hours=1)
        # Force expiry
        rec._d["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).isoformat()
        # Persist the modified state
        svc._save(rec._d)
        with pytest.raises(ValueError, match="expired"):
            svc.verify_token_for_read(rec.approval_id, rec.review_token)

    def test_decide_raises_on_expired(self, svc):
        rec = _create(svc, expires_in_hours=1)
        rec._d["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).isoformat()
        svc._save(rec._d)
        with pytest.raises(ValueError, match="expired"):
            svc.decide(rec.approval_id, decision="approved", comment=None, token=rec.review_token)

    def test_already_decided_is_not_re_blocked_by_expiry(self, svc):
        """verify_token_for_read should NOT raise for a decided (non-pending) record even if past expiry."""
        rec = _create(svc, expires_in_hours=1)
        svc.decide(rec.approval_id, decision="approved", comment=None, token=rec.review_token)
        # Force expiry on the decided record
        decided = svc.get(rec.approval_id)
        decided._d["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=5)
        ).isoformat()
        svc._save(decided._d)
        # Should NOT raise — expiry check in verify_token_for_read only applies to pending
        result = svc.verify_token_for_read(rec.approval_id, rec.review_token)
        assert result.status == "approved"


# ---------------------------------------------------------------------------
# Not-found
# ---------------------------------------------------------------------------

class TestNotFound:
    def test_get_missing_id_returns_none(self, svc):
        assert svc.get("appr_doesnotexist") is None

    def test_decide_missing_id_raises_key_error(self, svc):
        with pytest.raises(KeyError):
            svc.decide("appr_missing", decision="approved", comment=None, token="tok")

    def test_verify_token_for_read_missing_id_raises_key_error(self, svc):
        with pytest.raises(KeyError):
            svc.verify_token_for_read("appr_missing", "tok")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_create_requires_pdf_filename(self, svc):
        with pytest.raises(ValueError, match="pdf_filename"):
            svc.create(title="T", pdf_filename="", reviewer_email="x@y.com")

    def test_create_requires_reviewer_info(self, svc):
        with pytest.raises(ValueError, match="reviewer"):
            svc.create(title="T", pdf_filename="x.pdf", reviewer_email="", reviewer_name="")
