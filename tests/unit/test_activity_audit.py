"""Unit tests for clinic activity audit log (no PHI in entries)."""

from fillmypdf.services.activity_audit_service import (
    ActivityAuditService,
    classify_event,
    should_audit,
)


def test_classify_known_routes():
    assert classify_event("POST", "/api/v1/auth/login")[0] == "auth.login"
    assert classify_event("POST", "/api/v1/templates/tpl_abc/fill") == (
        "template.fill",
        "template",
        "tpl_abc",
    )
    assert classify_event("POST", "/api/v1/card-capture/extract")[0] == "card.capture"
    assert classify_event("POST", "/api/v1/note-paste/extract")[0] == "note.paste"
    assert classify_event("POST", "/api/v1/mappings/sig123/lock")[0] == "mapping.lock"


def test_skips_reads_and_audit_itself():
    assert should_audit("GET", "/api/v1/templates") is False
    assert should_audit("POST", "/api/v1/audit") is False
    assert should_audit("POST", "/api/v1/billing/webhook") is False
    assert should_audit("POST", "/api/v1/templates/x/fill") is True


def test_record_and_org_scope(tmp_path, monkeypatch):
    import fillmypdf.config as cfg

    monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path)
    svc = ActivityAuditService()
    svc.record(
        event="template.fill",
        method="POST",
        path="/api/v1/templates/t1/fill",
        status=200,
        actor_email="a@clinic.com",
        org_id="org_a",
        resource_type="template",
        resource_id="t1",
    )
    svc.record(
        event="template.fill",
        method="POST",
        path="/api/v1/templates/t2/fill",
        status=200,
        actor_email="b@other.com",
        org_id="org_b",
        resource_id="t2",
    )
    mine = svc.list_recent(org_id="org_a", admin=False)
    assert len(mine) == 1
    assert mine[0]["actor_email"] == "a@clinic.com"
    assert "user_data" not in mine[0]
    assert mine[0]["resource_id"] == "t1"
    all_ev = svc.list_recent(admin=True)
    assert len(all_ev) == 2


def test_event_prefix_filter(tmp_path, monkeypatch):
    import fillmypdf.config as cfg

    monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path)
    svc = ActivityAuditService()
    svc.record(event="auth.login", method="POST", path="/api/v1/auth/login", status=200, org_id="org_a")
    svc.record(event="template.fill", method="POST", path="/api/v1/templates/x/fill", status=200, org_id="org_a")
    only_auth = svc.list_recent(org_id="org_a", event="auth.", admin=False)
    assert [e["event"] for e in only_auth] == ["auth.login"]


def test_resource_filter_scopes_to_one_document(tmp_path, monkeypatch):
    """Document-specific history (the Mapping Review 'History' panel):
    filtering by resource_type + resource_id must return only that
    document's own events, not every mapping's."""
    import fillmypdf.config as cfg

    monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path)
    svc = ActivityAuditService()
    svc.record(
        event="mapping.lock", method="POST", path="/api/v1/mappings/fp1/lock",
        status=200, org_id="org_a", resource_type="mapping", resource_id="fp1",
    )
    svc.record(
        event="mapping.lock", method="POST", path="/api/v1/mappings/fp2/lock",
        status=200, org_id="org_a", resource_type="mapping", resource_id="fp2",
    )
    only_fp1 = svc.list_recent(admin=True, resource_type="mapping", resource_id="fp1")
    assert len(only_fp1) == 1
    assert only_fp1[0]["resource_id"] == "fp1"


class TestLockAndUploadIndex:
    """The Templates list's audit stamp (locked/added, who + when + REV) —
    a single pass over the whole log, not a scan per template row."""

    def test_lock_event_sets_locked_at_and_by(self, tmp_path, monkeypatch):
        import fillmypdf.config as cfg

        monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path)
        svc = ActivityAuditService()
        svc.record(
            event="mapping.lock", method="POST", path="/api/v1/mappings/fp1/lock",
            status=200, actor_email="samy@clinic.com",
            resource_type="mapping", resource_id="fp1",
        )
        mapping_idx, _ = svc.lock_and_upload_index()
        assert mapping_idx["fp1"]["locked_by"] == "samy@clinic.com"
        assert mapping_idx["fp1"]["locked_at"]
        assert mapping_idx["fp1"]["revision_count"] == 1

    def test_non_lock_events_still_count_toward_revisions(self, tmp_path, monkeypatch):
        import fillmypdf.config as cfg

        monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path)
        svc = ActivityAuditService()
        svc.record(
            event="mapping.lock", method="POST", path="/api/v1/mappings/fp1/lock",
            status=200, actor_email="samy@clinic.com", resource_type="mapping", resource_id="fp1",
        )
        svc.record(
            event="mapping.update", method="PATCH", path="/api/v1/mappings/fp1",
            status=200, actor_email="samy@clinic.com", resource_type="mapping", resource_id="fp1",
        )
        svc.record(
            event="mapping.checklist_update", method="PUT", path="/api/v1/mappings/fp1/checklist",
            status=200, actor_email="samy@clinic.com", resource_type="mapping", resource_id="fp1",
        )
        mapping_idx, _ = svc.lock_and_upload_index()
        assert mapping_idx["fp1"]["revision_count"] == 3
        # A later edit must NOT overwrite when/who actually locked it.
        assert mapping_idx["fp1"]["locked_by"] == "samy@clinic.com"

    def test_edit_after_lock_does_not_clobber_lock_timestamp(self, tmp_path, monkeypatch):
        """Regression for the exact gap the mapping cache's own actor/
        updated_at fields have: they get overwritten by ANY later edit."""
        import fillmypdf.config as cfg

        monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path)
        svc = ActivityAuditService()
        svc.record(
            event="mapping.lock", method="POST", path="/api/v1/mappings/fp1/lock",
            status=200, actor_email="samy@clinic.com", resource_type="mapping", resource_id="fp1",
        )
        svc.record(
            event="mapping.unlock", method="POST", path="/api/v1/mappings/fp1/unlock",
            status=200, actor_email="rena@clinic.com", resource_type="mapping", resource_id="fp1",
        )
        mapping_idx, _ = svc.lock_and_upload_index()
        # unlock is not a lock event — locked_at/by must still reflect the lock.
        assert mapping_idx["fp1"]["locked_by"] == "samy@clinic.com"

    def test_relock_updates_to_the_latest_lock(self, tmp_path, monkeypatch):
        import fillmypdf.config as cfg

        monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path)
        svc = ActivityAuditService()
        svc.record(
            event="mapping.lock", method="POST", path="/api/v1/mappings/fp1/lock",
            status=200, actor_email="samy@clinic.com", resource_type="mapping", resource_id="fp1",
        )
        svc.record(
            event="mapping.lock", method="POST", path="/api/v1/mappings/fp1/lock",
            status=200, actor_email="rena@clinic.com", resource_type="mapping", resource_id="fp1",
        )
        mapping_idx, _ = svc.lock_and_upload_index()
        assert mapping_idx["fp1"]["locked_by"] == "rena@clinic.com"

    def test_template_upload_sets_added_at_and_by(self, tmp_path, monkeypatch):
        import fillmypdf.config as cfg

        monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path)
        svc = ActivityAuditService()
        svc.record(
            event="template.upload", method="POST", path="/api/v1/templates",
            status=201, actor_email="rena@clinic.com", resource_type="template", resource_id="tpl_1",
        )
        _, template_idx = svc.lock_and_upload_index()
        assert template_idx["tpl_1"]["added_by"] == "rena@clinic.com"
        assert template_idx["tpl_1"]["added_at"]

    def test_no_events_returns_empty_indexes(self, tmp_path, monkeypatch):
        import fillmypdf.config as cfg

        monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path)
        svc = ActivityAuditService()
        mapping_idx, template_idx = svc.lock_and_upload_index()
        assert mapping_idx == {}
        assert template_idx == {}
