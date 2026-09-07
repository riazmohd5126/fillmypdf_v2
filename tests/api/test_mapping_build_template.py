"""Build a Mapping Review draft from a Template Library id."""

from io import BytesIO
from unittest.mock import patch

from pypdf import PdfWriter

from fillmypdf.models.template import TemplateManifest
from fillmypdf.services.canonical_field_service import CanonicalFieldService
from fillmypdf.services.canonical_map_cache import CanonicalMapCache
from fillmypdf.services.template_service import TemplateService


def _writer_bytes(writer: PdfWriter) -> bytes:
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _blank_fillable_bytes() -> bytes:
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
    return _writer_bytes(w)


def _flat_pdf_bytes() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    return _writer_bytes(w)


class _FakeVisionService:
    model = "test"

    def __init__(self, *args, **kwargs):
        self._canonical_service = CanonicalFieldService()

    @staticmethod
    def _widget_key(f: dict) -> str:
        return str(f.get("name") or "")

    def _get_fields_with_coords(self, path):
        return [{
            "name": "patient_name",
            "type": "/Tx",
            "page": 0,
            "x0": 72,
            "x1": 300,
            "x": 186,
            "y": 700,
            "y_bottom": 720,
        }]

    def rich_label_data(self, path, fields_info):
        return {}

    def _flatten_field_labels(self, fields_info, label_data):
        return {"patient_name": "Patient Name"}


def test_build_missing_template_returns_404(client, admin_api_key):
    r = client.post(
        "/api/v1/mappings/build",
        headers={"X-API-Key": admin_api_key["plain"]},
        data={"template_id": "priv_does_not_exist"},
    )
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_build_from_template_id_stamps_and_serves_pdf(client, admin_api_key, isolated_storage):
    tid = "priv_clinic_pa_abc123"
    TemplateService().add(
        TemplateManifest(id=tid, name="Clinic Aetna PA"),
        _blank_fillable_bytes(),
    )

    with patch("fillmypdf.services.vision_service.VisionService", _FakeVisionService):
        r = client.post(
            "/api/v1/mappings/build",
            headers={"X-API-Key": admin_api_key["plain"]},
            data={"template_id": tid},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["template_id"] == tid
    assert body["form_label"] == "Clinic Aetna PA"
    assert body["has_blank_pdf"] is True
    fp = body["fingerprint"]
    assert fp

    stored = CanonicalMapCache().get_full(fp)
    assert stored is not None
    assert stored.get("template_id") == tid

    pdf = client.get(
        f"/api/v1/mappings/{fp}/pdf",
        headers={"X-API-Key": admin_api_key["plain"]},
    )
    assert pdf.status_code == 200
    assert pdf.headers.get("content-type", "").startswith("application/pdf")


def test_readiness_matches_stamped_template_id(client, admin_api_key, isolated_storage):
    tid = "priv_clinic_form_xyz789"
    TemplateService().add(
        TemplateManifest(id=tid, name="Unrelated Display Name"),
        b"%PDF-1.4\n%%EOF\n",
    )
    CanonicalMapCache().save_full(
        "fp_clinic_xyz",
        {
            "fingerprint": "fp_clinic_xyz",
            "signature": "sig_clinic",
            "form_label": "upload.pdf",
            "template_id": tid,
            "reviewed": True,
            "mappings": {"a": {"canonical": "patient.name"}},
            "field_labels": {"a": "Name"},
        },
    )
    r = client.get(
        "/api/v1/templates/readiness",
        headers={"X-API-Key": admin_api_key["plain"]},
    )
    assert r.status_code == 200
    hit = next(x for x in r.json()["items"] if x["template_id"] == tid)
    assert hit["ready"] is True
    assert hit["fingerprint"] == "fp_clinic_xyz"


def test_unmapped_template_reports_not_ready(client, admin_api_key, isolated_storage):
    """The Mapping Review rail builds its queue from these two responses."""
    tid = "priv_clinic_unmapped_001"
    TemplateService().add(
        TemplateManifest(id=tid, name="Brand New Clinic Upload"),
        _blank_fillable_bytes(),
    )
    headers = {"X-API-Key": admin_api_key["plain"]}

    listing = client.get("/api/v1/templates", headers=headers)
    assert listing.status_code == 200
    assert any(t["id"] == tid for t in listing.json()["templates"])

    readiness = client.get("/api/v1/templates/readiness", headers=headers)
    assert readiness.status_code == 200
    hit = next(x for x in readiness.json()["items"] if x["template_id"] == tid)
    assert hit["ready"] is False
    assert not hit.get("fingerprint")


def test_flat_template_error_explains_conversion(client, admin_api_key, isolated_storage):
    """A flat PDF must fail with the real cause, not a bare AcroForm message."""
    tid = "priv_clinic_flat_002"
    TemplateService().add(
        TemplateManifest(id=tid, name="Scanned Intake Form"),
        _flat_pdf_bytes(),
    )

    class _NoFields(_FakeVisionService):
        def _get_fields_with_coords(self, path):
            return []

    with patch("fillmypdf.services.vision_service.VisionService", _NoFields), patch.object(
        TemplateService, "_ensure_fillable", return_value=None
    ):
        r = client.post(
            "/api/v1/mappings/build",
            headers={"X-API-Key": admin_api_key["plain"]},
            data={"template_id": tid},
        )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Scanned Intake Form" in detail
    assert "Make Fillable" in detail
