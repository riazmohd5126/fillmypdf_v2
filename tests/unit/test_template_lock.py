"""Lock-before-fill and blank-PDF mapping guards."""

from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

from fillmypdf.services.template_access import acroform_has_filled_values


def _blank_fillable(path: Path) -> Path:
    w = PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    w.add_annotation(
        page,
        {
            "/Subtype": "/Widget",
            "/FT": "/Tx",
            "/T": "patient_name",
            "/Rect": [72, 700, 300, 720],
        },
    )
    path.write_bytes(_writer_bytes(w))
    return path


def _filled_fillable(path: Path) -> Path:
    w = PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    w.add_annotation(
        page,
        {
            "/Subtype": "/Widget",
            "/FT": "/Tx",
            "/T": "patient_name",
            "/V": "Jane Doe",
            "/Rect": [72, 700, 300, 720],
        },
    )
    path.write_bytes(_writer_bytes(w))
    return path


def _writer_bytes(writer: PdfWriter) -> bytes:
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_blank_acroform_is_not_filled(tmp_path):
    p = _blank_fillable(tmp_path / "blank.pdf")
    assert acroform_has_filled_values(p) is False


def test_filled_acroform_is_detected(tmp_path):
    p = _filled_fillable(tmp_path / "filled.pdf")
    assert acroform_has_filled_values(p) is True


def test_mapping_build_rejects_filled_pdf(client, admin_api_key, tmp_path):
    pdf = _filled_fillable(tmp_path / "phi.pdf")
    r = client.post(
        "/api/v1/mappings/build",
        headers={"X-API-Key": admin_api_key["plain"]},
        files={"file": ("form.pdf", pdf.read_bytes(), "application/pdf")},
    )
    assert r.status_code == 400
    assert "blank" in r.json()["detail"].lower() or "filled" in r.json()["detail"].lower()


def test_adhoc_batch_requires_admin(client, free_api_key):
    r = client.post(
        "/api/v1/jobs/batch",
        headers={"X-API-Key": free_api_key["plain"]},
        data={"ai_api_key": "k", "records": '[{"a":1}]'},
        files={"file": ("pa.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert r.status_code == 403
