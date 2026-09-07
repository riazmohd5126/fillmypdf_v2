"""Make Fillable must not report a field-less PDF as a success."""

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from fillmypdf.api.routes import pdf_utils_routes


def _flat_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


def _fake_report(status: str, fields_after: int):
    def _run(input_path, output_path):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(Path(input_path).read_bytes())
        return {
            "ok": True,
            "status": status,
            "engine": "cloud",
            "field_count_before": 0,
            "field_count_after": fields_after,
            "page_count": 1,
            "message": "stub",
        }

    return _run


def _post(client, admin_api_key):
    return client.post(
        "/api/v1/pdf/convert-fillable",
        headers={"X-API-Key": admin_api_key["plain"]},
        files={"file": ("intake.pdf", _flat_pdf(), "application/pdf")},
    )


def test_copied_as_is_returns_a_warning(client, admin_api_key, isolated_storage):
    with patch.object(
        pdf_utils_routes._pdf_service,
        "convert_to_fillable_detailed",
        _fake_report("copied_as_is", 0),
    ):
        r = _post(client, admin_api_key)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "copied_as_is"
    assert "No fillable fields were detected" in (body.get("warning") or "")


def test_successful_conversion_has_no_warning(client, admin_api_key, isolated_storage):
    with patch.object(
        pdf_utils_routes._pdf_service,
        "convert_to_fillable_detailed",
        _fake_report("converted", 42),
    ):
        r = _post(client, admin_api_key)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["field_count_after"] == 42
    assert not body.get("warning")
