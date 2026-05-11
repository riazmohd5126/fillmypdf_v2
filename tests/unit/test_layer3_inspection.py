"""
Layer 3 — inspect_fillable_form (no AI) and batch `/template-fields` route (mocked).
"""

from pathlib import Path

import pytest


@pytest.fixture
def vision():
    """VisionService fixture without calling OpenAI."""
    from fillmypdf.services.vision_service import VisionService

    return VisionService(api_key="unused", base_url="http://unused", model="unused")


def test_inspect_fillable_form_merges_rows(vision, monkeypatch):
    sample_fields = [
        {
            "name": "Fld1",
            "type": "/Tx",
            "page": 0,
            "x0": 140,
            "x1": 300,
            "x": 220,
            "y": 90,
        },
        {
            "name": "Chk_A",
            "type": "/Btn",
            "page": 0,
            "x0": 30,
            "x1": 42,
            "x": 36,
            "y": 120,
        },
    ]

    monkeypatch.setattr(
        vision, "_get_fields_with_coords", lambda path: sample_fields.copy()
    )
    monkeypatch.setattr(
        vision,
        "_extract_labels_for_fields",
        lambda pdf_path, fields_info: {
            "Fld1": "Member name",
            "Chk_A": "Prior therapy",
        },
    )

    out = vision.inspect_fillable_form("/fake/fillable.pdf")

    assert out["fields_detected"] == 2
    assert out["fields"][0]["field_type"] == "text"
    assert out["fields"][0]["label"] == "Member name"
    assert out["fields"][1]["field_type"] == "checkbox"


def test_inspect_empty_pdf_returns_zero(vision, monkeypatch):
    monkeypatch.setattr(vision, "_get_fields_with_coords", lambda path: [])
    out = vision.inspect_fillable_form("/x.pdf")
    assert out["fields_detected"] == 0


def test_analyze_template_fields_via_api(client, auth_headers_free, monkeypatch):
    pytest.importorskip("fillmypdf.api.routes.batch_routes")

    openapi = client.get("/openapi.json").json()
    if "/api/v1/batch/template-fields" not in (openapi.get("paths") or {}):
        pytest.skip("batch router not mounted (HAS_BATCH=false)")

    import fillmypdf.api.routes.batch_routes as br

    def fake_analyze(self, template_pdf_path: Path):
        assert template_pdf_path.exists()
        return {
            "success": True,
            "fields_detected": 1,
            "fields": [
                {
                    "name": "t1",
                    "field_type": "text",
                    "page": 0,
                    "label": "Name",
                    "x0": 0,
                    "x1": 10,
                    "y": 5,
                }
            ],
            "message": None,
        }

    monkeypatch.setattr(br.BatchFillService, "analyze_template_fields", fake_analyze)

    r = client.post(
        "/api/v1/batch/template-fields",
        headers=auth_headers_free,
        files={"file": ("x.pdf", b"%PDF-1.4 minimal", "application/pdf")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["fields_detected"] == 1
    assert data["fields"][0]["name"] == "t1"
    assert (
        r.headers.get("X-Request-ID") or r.headers.get("x-request-id") or ""
    ) != ""
