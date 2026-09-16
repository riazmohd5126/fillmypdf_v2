"""
GET /api/v1/templates/readiness — audit-derived fields
==========================================================
The Templates list's "Audit Stamp" (locked/added, who + when + REV) reads
locked_at/locked_by/revision_count/added_at/added_by off this response —
covers the real end-to-end path (real lock, real upload) so a change to
either route can't silently break what the list displays.
"""

import json

from fillmypdf.models.template import TemplateManifest
from fillmypdf.services.canonical_map_cache import CanonicalMapCache
from fillmypdf.services.form_spec_cache import FormSpecCache
from fillmypdf.services.template_service import TemplateService


def _seed_mapping(tid: str, fp: str, sig: str) -> None:
    TemplateService().add(TemplateManifest(id=tid, name="Audit Stamp Test Form"), b"%PDF-1.4\n%%EOF\n")
    CanonicalMapCache().save_full(
        fp,
        {
            "fingerprint": fp,
            "signature": sig,
            "form_label": "Audit Stamp Test Form",
            "template_id": tid,
            "reviewed": False,
            "mappings": {"a": {"canonical": "patient.name"}},
            "field_labels": {"a": "Name"},
        },
    )
    from fillmypdf.models.form_spec import FormSpec

    FormSpecCache().save(FormSpec(signature=sig, form_label="Audit Stamp Test Form"))


class TestReadinessAuditFields:
    def test_locked_template_reports_locked_at_and_by(self, client, admin_api_key, isolated_storage):
        _seed_mapping("priv_stamp_001", "fp_stamp_001", "sig_stamp_001")
        headers = {"X-API-Key": admin_api_key["plain"]}

        lock = client.post("/api/v1/mappings/fp_stamp_001/lock", headers=headers)
        assert lock.status_code == 200, lock.text

        r = client.get("/api/v1/templates/readiness", headers=headers)
        assert r.status_code == 200, r.text
        item = next(x for x in r.json()["items"] if x["template_id"] == "priv_stamp_001")
        assert item["ready"] is True
        assert item["locked_at"]
        assert item["locked_by"]
        assert item["revision_count"] >= 1

    def test_edit_after_lock_does_not_change_locked_by(self, client, admin_api_key, isolated_storage):
        """The whole point: the mapping cache's own actor field gets
        clobbered by a later edit, but locked_at/by must not."""
        _seed_mapping("priv_stamp_002", "fp_stamp_002", "sig_stamp_002")
        headers = {"X-API-Key": admin_api_key["plain"]}

        client.post("/api/v1/mappings/fp_stamp_002/lock", headers=headers)
        before = client.get("/api/v1/templates/readiness", headers=headers).json()
        before_item = next(x for x in before["items"] if x["template_id"] == "priv_stamp_002")

        # Unlock, then a later action touches the map again.
        client.post("/api/v1/mappings/fp_stamp_002/unlock", headers=headers)
        client.put(
            "/api/v1/mappings/fp_stamp_002/checklist",
            headers=headers,
            json={"checklist": ["Some item"]},
        )

        after = client.get("/api/v1/templates/readiness", headers=headers).json()
        after_item = next(x for x in after["items"] if x["template_id"] == "priv_stamp_002")
        assert after_item["locked_at"] == before_item["locked_at"]
        assert after_item["locked_by"] == before_item["locked_by"]
        assert after_item["ready"] is False  # unlocked
        assert after_item["revision_count"] > before_item["revision_count"]

    def test_unlocked_template_has_no_lock_stamp(self, client, admin_api_key, isolated_storage):
        _seed_mapping("priv_stamp_003", "fp_stamp_003", "sig_stamp_003")
        headers = {"X-API-Key": admin_api_key["plain"]}
        r = client.get("/api/v1/templates/readiness", headers=headers)
        item = next(x for x in r.json()["items"] if x["template_id"] == "priv_stamp_003")
        assert item["ready"] is False
        assert item["locked_at"] is None
        assert item["locked_by"] is None

    def test_real_upload_sets_added_by_from_audit_log(self, client, admin_api_key, isolated_storage):
        headers = {"X-API-Key": admin_api_key["plain"]}
        manifest = {"id": "priv_stamp_004", "name": "Uploaded via real endpoint"}
        r = client.post(
            "/api/v1/templates",
            headers=headers,
            files={"file": ("form.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")},
            data={"manifest_json": json.dumps(manifest)},
        )
        assert r.status_code == 201, r.text

        ready = client.get("/api/v1/templates/readiness", headers=headers)
        item = next(x for x in ready.json()["items"] if x["template_id"] == "priv_stamp_004")
        assert item["added_at"]
        assert item["added_by"]

    def test_template_predating_audit_log_falls_back_to_created_at(self, client, admin_api_key, isolated_storage):
        """Seeded directly (as in every other fixture here) rather than via
        the real upload endpoint — no template.upload audit event exists,
        so this must fall back to the manifest's own created_at instead of
        leaving added_at empty."""
        TemplateService().add(TemplateManifest(id="priv_stamp_005", name="No audit event"), b"%PDF-1.4\n%%EOF\n")
        headers = {"X-API-Key": admin_api_key["plain"]}
        r = client.get("/api/v1/templates/readiness", headers=headers)
        item = next(x for x in r.json()["items"] if x["template_id"] == "priv_stamp_005")
        assert item["added_at"]
        assert item["added_by"] is None
