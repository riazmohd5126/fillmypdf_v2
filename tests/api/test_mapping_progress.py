"""
GET /api/v1/templates/mapping-progress/{fp}
==============================================
A deliberately small, read-only slice of the admin-only Mapping Review
detail, available to any signed-in user (not just admins) — powers the
Dashboard queue's mapping-progress panel for clinic users, not just admins.
"""

from fillmypdf.models.template import TemplateManifest
from fillmypdf.services.canonical_map_cache import CanonicalMapCache
from fillmypdf.services.template_service import TemplateService


def _seed_mapping(fp: str) -> None:
    TemplateService().add(TemplateManifest(id="prog_tmpl_001", name="Progress Test Form"), b"%PDF-1.4\n%%EOF\n")
    CanonicalMapCache().save_full(
        fp,
        {
            "fingerprint": fp,
            "signature": "sig_prog_001",
            "form_label": "Progress Test Form",
            "template_id": "prog_tmpl_001",
            "reviewed": False,
            "mappings": {
                "a": {"canonical": "patient.name"},
                "b": {"canonical": "other", "confidence": 0.4},
                "c": {"confidence": 0.9},
            },
            "field_labels": {"a": "Name", "b": "Field B", "c": "Field C"},
            "field_types": {"a": "text", "b": "text", "c": "checkbox"},
        },
    )


class TestMappingProgress:
    def test_non_admin_can_read_progress(self, client, auth_headers_pro, isolated_storage):
        _seed_mapping("fp_prog_001")
        r = client.get("/api/v1/templates/mapping-progress/fp_prog_001", headers=auth_headers_pro)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["fingerprint"] == "fp_prog_001"
        assert body["form_label"] == "Progress Test Form"
        assert body["reviewed"] is False
        assert body["field_count"] == 3

    def test_unresolved_excludes_mapped_fields(self, client, auth_headers_pro, isolated_storage):
        _seed_mapping("fp_prog_002")
        r = client.get("/api/v1/templates/mapping-progress/fp_prog_002", headers=auth_headers_pro)
        assert r.status_code == 200, r.text
        unresolved_fields = {row["field"] for row in r.json()["unresolved"]}
        assert unresolved_fields == {"b", "c"}
        assert "a" not in unresolved_fields

    def test_unresolved_rows_have_no_catalog_or_editing_data(self, client, auth_headers_pro, isolated_storage):
        _seed_mapping("fp_prog_003")
        r = client.get("/api/v1/templates/mapping-progress/fp_prog_003", headers=auth_headers_pro)
        body = r.json()
        assert "catalog" not in body
        assert "form_spec" not in body
        assert "rows" not in body
        for row in body["unresolved"]:
            assert set(row.keys()) == {"field", "label", "field_type", "confidence"}

    def test_unknown_fingerprint_404s(self, client, auth_headers_pro, isolated_storage):
        r = client.get("/api/v1/templates/mapping-progress/does-not-exist", headers=auth_headers_pro)
        assert r.status_code == 404

    def test_requires_auth(self, client, isolated_storage):
        r = client.get("/api/v1/templates/mapping-progress/fp_prog_001")
        assert r.status_code in (401, 403)
