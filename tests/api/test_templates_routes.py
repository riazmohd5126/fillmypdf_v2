"""
API integration tests — /api/v1/templates
==========================================
All heavy operations (VisionService, PDFService) are mocked so tests run
instantly without needing real PDFs, AI keys, or CommonForms models.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from fillmypdf.models.template import (
    TemplateListItem,
    TemplateListResponse,
    TemplateManifest,
    TemplateDrug,
    TemplatePayer,
    TemplateQuestion,
    TemplateFillResponse,
    TemplateBatchResponse,
)

BASE = "/api/v1/templates"

FAKE_MANIFEST = TemplateManifest(
    id="pa_linzess_test",
    name="Linzess PA Test",
    category="prior_authorization",
    specialty="gi_motility",
    drug=TemplateDrug(name="Linzess", generic_name="linaclotide", strengths=["145mcg"]),
    payer=TemplatePayer(name="Molina TX", plan_type="medicaid", state="TX"),
    indications=["ibs-c"],
    questions=[TemplateQuestion(key="q1", text="Is patient 18+?")],
    tags=["medicaid", "gi"],
    pages=2,
)

FAKE_LIST_ITEM = TemplateListItem(
    id="pa_linzess_test",
    name="Linzess PA Test",
    category="prior_authorization",
    drug_name="Linzess",
    payer_name="Molina TX",
    plan_type="medicaid",
    state="TX",
    specialty="gi_motility",
    tags=["medicaid", "gi"],
    pages=2,
    question_count=1,
)

FAKE_FILL_RESP = TemplateFillResponse(
    success=True,
    template_id="pa_linzess_test",
    fields_detected=8,
    fields_filled=7,
    avg_confidence=0.91,
    cache_hit=False,
    download_url="/api/v1/templates/download/filled_test.pdf",
)

FAKE_BATCH_RESP = TemplateBatchResponse(
    success=True,
    template_id="pa_linzess_test",
    batch_id="tmpl_abc123",
    total_records=2,
    successful=2,
    failed=0,
    success_rate=100.0,
    cache_hits=1,
    avg_confidence=0.88,
    download_url="/api/v1/templates/download/batch_test.zip",
)


def _plain(key_dict: dict) -> str:
    return key_dict["plain"]


# ---------------------------------------------------------------------------
# List templates
# ---------------------------------------------------------------------------


class TestListTemplates:

    def test_list_returns_templates(self, client, free_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.list.return_value = [FAKE_LIST_ITEM]
            resp = client.get(BASE, headers={"X-API-Key": _plain(free_api_key)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["templates"][0]["id"] == "pa_linzess_test"

    def test_list_empty(self, client, free_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.list.return_value = []
            resp = client.get(BASE, headers={"X-API-Key": _plain(free_api_key)})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_list_passes_filters(self, client, free_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.list.return_value = [FAKE_LIST_ITEM]
            resp = client.get(
                BASE + "?drug=linzess&state=TX",
                headers={"X-API-Key": _plain(free_api_key)},
            )
        assert resp.status_code == 200
        # Verify the service was called with the filters
        call_kwargs = mock.return_value.list.call_args.kwargs
        assert call_kwargs.get("drug") == "linzess"
        assert call_kwargs.get("state") == "TX"

    def test_list_requires_auth(self, client):
        resp = client.get(BASE)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Get manifest
# ---------------------------------------------------------------------------


class TestGetTemplate:

    def test_get_existing(self, client, free_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.return_value = FAKE_MANIFEST
            resp = client.get(
                f"{BASE}/pa_linzess_test",
                headers={"X-API-Key": _plain(free_api_key)},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "pa_linzess_test"
        assert body["drug"]["name"] == "Linzess"
        assert len(body["questions"]) == 1

    def test_get_missing_returns_404(self, client, free_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.side_effect = KeyError("not found")
            resp = client.get(
                f"{BASE}/ghost",
                headers={"X-API-Key": _plain(free_api_key)},
            )
        assert resp.status_code == 404

    def test_get_requires_auth(self, client):
        resp = client.get(f"{BASE}/pa_linzess_test")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Inspect fields
# ---------------------------------------------------------------------------


class TestInspectFields:

    def test_inspect_existing(self, client, free_api_key):
        fake_fields = {"fields_detected": 5, "fields": []}
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.return_value = FAKE_MANIFEST
            mock.return_value.inspect_fields.return_value = fake_fields
            resp = client.get(
                f"{BASE}/pa_linzess_test/fields",
                headers={"X-API-Key": _plain(free_api_key)},
            )
        assert resp.status_code == 200
        assert resp.json()["fields_detected"] == 5

    def test_inspect_missing_returns_404(self, client, free_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.side_effect = KeyError("not found")
            resp = client.get(
                f"{BASE}/ghost/fields",
                headers={"X-API-Key": _plain(free_api_key)},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Fill — single record
# ---------------------------------------------------------------------------


class TestFillTemplate:

    def test_fill_success(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.return_value = FAKE_MANIFEST
            mock.return_value.fill.return_value = FAKE_FILL_RESP
            resp = client.post(
                f"{BASE}/pa_linzess_test/fill",
                data={
                    "ai_api_key": "test-key",
                    "user_data": json.dumps({"first_name": "Jane", "last_name": "Doe"}),
                },
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["fields_filled"] == 7
        assert "download_url" in body

    def test_fill_invalid_json_returns_400(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.return_value = FAKE_MANIFEST
            resp = client.post(
                f"{BASE}/pa_linzess_test/fill",
                data={"ai_api_key": "k", "user_data": "not json"},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400

    def test_fill_missing_template_returns_404(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.side_effect = KeyError("not found")
            resp = client.post(
                f"{BASE}/ghost/fill",
                data={"ai_api_key": "k", "user_data": "{}"},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Batch fill
# ---------------------------------------------------------------------------


class TestBatchFillTemplate:

    def test_batch_success(self, client, pro_api_key):
        records = [{"first_name": "Alice"}, {"first_name": "Bob"}]
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.return_value = FAKE_MANIFEST
            mock.return_value.fill_batch.return_value = FAKE_BATCH_RESP
            resp = client.post(
                f"{BASE}/pa_linzess_test/batch",
                data={
                    "ai_api_key": "test-key",
                    "records": json.dumps(records),
                },
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["successful"] == 2
        assert body["cache_hits"] == 1

    def test_batch_empty_array_returns_400(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.return_value = FAKE_MANIFEST
            resp = client.post(
                f"{BASE}/pa_linzess_test/batch",
                data={"ai_api_key": "k", "records": "[]"},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400

    def test_batch_over_500_returns_400(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.return_value = FAKE_MANIFEST
            records = [{"first_name": f"Patient{i}"} for i in range(501)]
            resp = client.post(
                f"{BASE}/pa_linzess_test/batch",
                data={"ai_api_key": "k", "records": json.dumps(records)},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 400

    def test_batch_missing_template_returns_404(self, client, pro_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.get.side_effect = KeyError("not found")
            resp = client.post(
                f"{BASE}/ghost/batch",
                data={"ai_api_key": "k", "records": "[{}]"},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Admin: upload
# ---------------------------------------------------------------------------


class TestUploadTemplate:

    def _manifest_json(self, tid="pa_new"):
        return json.dumps({"id": tid, "name": "New PA Form"})

    def test_upload_requires_admin(self, client, free_api_key):
        resp = client.post(
            BASE,
            data={"manifest_json": self._manifest_json()},
            files={"file": ("form.pdf", b"%PDF test", "application/pdf")},
            headers={"X-API-Key": _plain(free_api_key)},
        )
        assert resp.status_code == 403

    def test_upload_success(self, client, admin_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.add.return_value = FAKE_MANIFEST
            resp = client.post(
                BASE,
                data={"manifest_json": self._manifest_json()},
                files={"file": ("form.pdf", b"%PDF test", "application/pdf")},
                headers={"X-API-Key": _plain(admin_api_key)},
            )
        assert resp.status_code == 201

    def test_upload_non_pdf_returns_400(self, client, admin_api_key):
        with patch("fillmypdf.api.routes.templates._get_service"):
            resp = client.post(
                BASE,
                data={"manifest_json": self._manifest_json()},
                files={"file": ("form.docx", b"word doc", "application/msword")},
                headers={"X-API-Key": _plain(admin_api_key)},
            )
        assert resp.status_code == 400

    def test_upload_invalid_json_returns_400(self, client, admin_api_key):
        with patch("fillmypdf.api.routes.templates._get_service"):
            resp = client.post(
                BASE,
                data={"manifest_json": "not json"},
                files={"file": ("form.pdf", b"%PDF", "application/pdf")},
                headers={"X-API-Key": _plain(admin_api_key)},
            )
        assert resp.status_code == 400

    def test_upload_duplicate_returns_409(self, client, admin_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.add.side_effect = ValueError("already exists")
            resp = client.post(
                BASE,
                data={"manifest_json": self._manifest_json()},
                files={"file": ("form.pdf", b"%PDF", "application/pdf")},
                headers={"X-API-Key": _plain(admin_api_key)},
            )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Admin: delete
# ---------------------------------------------------------------------------


class TestDeleteTemplate:

    def test_delete_success(self, client, admin_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.delete.return_value = True
            resp = client.delete(
                f"{BASE}/pa_linzess_test",
                headers={"X-API-Key": _plain(admin_api_key)},
            )
        assert resp.status_code == 204

    def test_delete_missing_returns_404(self, client, admin_api_key):
        with patch("fillmypdf.api.routes.templates._get_service") as mock:
            mock.return_value.delete.return_value = False
            resp = client.delete(
                f"{BASE}/ghost",
                headers={"X-API-Key": _plain(admin_api_key)},
            )
        assert resp.status_code == 404

    def test_delete_requires_admin(self, client, free_api_key):
        resp = client.delete(
            f"{BASE}/pa_linzess_test",
            headers={"X-API-Key": _plain(free_api_key)},
        )
        assert resp.status_code == 403

    def test_delete_no_auth_returns_401(self, client):
        resp = client.delete(f"{BASE}/pa_linzess_test")
        assert resp.status_code == 401
