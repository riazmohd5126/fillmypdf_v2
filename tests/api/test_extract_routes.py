"""API tests for POST /api/v1/extract"""
import unittest.mock as mock

import pytest

from fillmypdf.models.extract import ExtractFieldItem, PdfExtractResponse

BASE = "/api/v1/extract"


def _plain(k):
    return k["plain"]


@pytest.fixture
def sample_extract_response():
    return PdfExtractResponse(
        success=True,
        fields_detected=2,
        non_empty_fields=2,
        filename="filled.pdf",
        fields=[
            ExtractFieldItem(name="f1", label="Patient", value="Jane", page=1, field_type="text"),
            ExtractFieldItem(name="f2", label=None, value="YES", page=1, field_type="checkbox"),
        ],
    )


class TestExtractJson:
    def test_returns_json(self, client, pro_api_key, sample_extract_response):
        with mock.patch("fillmypdf.api.routes.extract_routes._svc") as svc_mk:
            svc_mk.return_value.extract_pdf.return_value = sample_extract_response
            resp = client.post(
                BASE,
                params={"include_labels": "true", "format": "json"},
                files={"file": ("t.pdf", b"%PDF-1.4", "application/pdf")},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["fields_detected"] == 2
        assert data["fields"][0]["name"] == "f1"

    def test_non_pdf_returns_400(self, client, pro_api_key):
        resp = client.post(
            BASE,
            files={"file": ("bad.txt", b"hi", "text/plain")},
            headers={"X-API-Key": _plain(pro_api_key)},
        )
        assert resp.status_code == 400

    def test_requires_auth(self, client):
        resp = client.post(BASE, files={"file": ("x.pdf", b"%PDF")})
        assert resp.status_code == 401


class TestExtractCsv:
    def test_returns_csv_attachment(self, client, pro_api_key, sample_extract_response):
        with mock.patch("fillmypdf.api.routes.extract_routes._svc") as svc_mk:
            svc_mk.return_value.extract_pdf.return_value = sample_extract_response
            resp = client.post(
                BASE,
                params={"format": "csv"},
                files={"file": ("t.pdf", b"%PDF-1.4", "application/pdf")},
                headers={"X-API-Key": _plain(pro_api_key)},
            )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")
        assert "attachment" in resp.headers.get("content-disposition", "")
        body = resp.content.decode("utf-8").lstrip("\ufeff")
        assert "name," in body
        assert "Jane" in body
