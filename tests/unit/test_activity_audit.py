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
