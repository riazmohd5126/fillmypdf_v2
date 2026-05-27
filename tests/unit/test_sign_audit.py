"""Tests for signing audit trail."""

from fillmypdf.services.sign_audit_service import SignAuditService


def test_sign_audit_record_and_list(tmp_path, monkeypatch):
    import fillmypdf.config as cfg

    monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path)
    svc = SignAuditService()
    aid = svc.record(
        output_filename="signed_x.pdf",
        download_url="/api/v1/batch/download/signed_x.pdf",
        page_index=0,
        signature_mode="typed",
        signer_name="Pat",
        api_key_id="key_test",
    )
    assert aid.startswith("sig_")
    events = svc.list_recent(limit=10)
    assert len(events) == 1
    assert events[0]["audit_id"] == aid
    assert events[0]["event"] == "signature.applied"
    assert events[0]["signer_name"] == "Pat"
