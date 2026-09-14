"""
Mapping Review — submission checklist routes
==============================================
POST /{fp}/checklist/ai-suggest, PUT /{fp}/checklist, and the sync onto the
linked Template Library manifest that happens when the map is locked.
"""

from unittest.mock import patch

from fillmypdf.models.form_spec import FormSpec
from fillmypdf.models.template import TemplateManifest
from fillmypdf.services.canonical_map_cache import CanonicalMapCache
from fillmypdf.services.form_spec_cache import FormSpecCache
from fillmypdf.services.template_service import TemplateService


def _seed_mapping(tid: str, fp: str, sig: str) -> None:
    TemplateService().add(TemplateManifest(id=tid, name="Clinic Aetna PA"), b"%PDF-1.4\n%%EOF\n")
    CanonicalMapCache().save_full(
        fp,
        {
            "fingerprint": fp,
            "signature": sig,
            "form_label": "Clinic Aetna PA",
            "template_id": tid,
            "reviewed": False,
            "mappings": {"a": {"canonical": "patient.name"}},
            "field_labels": {"a": "Name"},
        },
    )
    FormSpecCache().save(FormSpec(signature=sig, form_label="Clinic Aetna PA"))


class TestChecklistEdit:
    def test_put_checklist_saves_and_returns_spec(self, client, admin_api_key, isolated_storage):
        _seed_mapping("priv_chk_001", "fp_chk_001", "sig_chk_001")
        r = client.put(
            "/api/v1/mappings/fp_chk_001/checklist",
            headers={"X-API-Key": admin_api_key["plain"]},
            json={"checklist": ["Chart notes", "Lab results"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["checklist"] == ["Chart notes", "Lab results"]

    def test_put_checklist_unknown_signature_404s(self, client, admin_api_key, isolated_storage):
        r = client.put(
            "/api/v1/mappings/fp_does_not_exist/checklist",
            headers={"X-API-Key": admin_api_key["plain"]},
            json={"checklist": ["item"]},
        )
        assert r.status_code == 404

    def test_ai_suggest_writes_draft_into_form_spec(self, client, admin_api_key, isolated_storage):
        _seed_mapping("priv_chk_002", "fp_chk_002", "sig_chk_002")
        with patch(
            "fillmypdf.api.routes.mapping_review_routes.settings.GEMINI_API_KEY", "fake-test-key"
        ), patch(
            "fillmypdf.services.checklist_service.ChecklistService.draft",
            return_value=["AI item one", "AI item two"],
        ):
            r = client.post(
                "/api/v1/mappings/fp_chk_002/checklist/ai-suggest",
                headers={"X-API-Key": admin_api_key["plain"]},
            )
        assert r.status_code == 200, r.text
        assert r.json()["checklist"] == ["AI item one", "AI item two"]

    def test_ai_suggest_without_configured_key_returns_409(self, client, admin_api_key, isolated_storage):
        _seed_mapping("priv_chk_006", "fp_chk_006", "sig_chk_006")
        r = client.post(
            "/api/v1/mappings/fp_chk_006/checklist/ai-suggest",
            headers={"X-API-Key": admin_api_key["plain"]},
        )
        assert r.status_code == 409


class TestChecklistSyncOnLock:
    def test_lock_publishes_checklist_onto_template_manifest(self, client, admin_api_key, isolated_storage):
        _seed_mapping("priv_chk_003", "fp_chk_003", "sig_chk_003")
        FormSpecCache().set_checklist("sig_chk_003", ["Chart notes documenting prior DMARD trial"])

        # Manifest has no checklist yet — it's still draft, unlocked.
        before = client.get(
            "/api/v1/templates/priv_chk_003",
            headers={"X-API-Key": admin_api_key["plain"]},
        )
        assert before.json()["checklist"] == []

        lock = client.post(
            "/api/v1/mappings/fp_chk_003/lock",
            headers={"X-API-Key": admin_api_key["plain"]},
        )
        assert lock.status_code == 200, lock.text

        after = client.get(
            "/api/v1/templates/priv_chk_003",
            headers={"X-API-Key": admin_api_key["plain"]},
        )
        assert after.json()["checklist"] == ["Chart notes documenting prior DMARD trial"]

    def test_lock_with_no_checklist_leaves_manifest_checklist_empty(
        self, client, admin_api_key, isolated_storage
    ):
        _seed_mapping("priv_chk_004", "fp_chk_004", "sig_chk_004")
        lock = client.post(
            "/api/v1/mappings/fp_chk_004/lock",
            headers={"X-API-Key": admin_api_key["plain"]},
        )
        assert lock.status_code == 200

        after = client.get(
            "/api/v1/templates/priv_chk_004",
            headers={"X-API-Key": admin_api_key["plain"]},
        )
        assert after.json()["checklist"] == []

    def test_lock_batch_also_syncs_checklist(self, client, admin_api_key, isolated_storage):
        _seed_mapping("priv_chk_005", "fp_chk_005", "sig_chk_005")
        FormSpecCache().set_checklist("sig_chk_005", ["Item via lock-batch"])

        r = client.post(
            "/api/v1/mappings/lock-batch",
            headers={"X-API-Key": admin_api_key["plain"]},
            json={"fingerprints": ["fp_chk_005"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["locked"] == 1

        after = client.get(
            "/api/v1/templates/priv_chk_005",
            headers={"X-API-Key": admin_api_key["plain"]},
        )
        assert after.json()["checklist"] == ["Item via lock-batch"]
