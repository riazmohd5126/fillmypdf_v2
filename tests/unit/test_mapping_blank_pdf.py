"""Mapping Review blank-PDF resolve: template library fallback + label match."""

from fillmypdf.api.routes.mapping_review_routes import (
    _resolve_blank_pdf,
    _template_id_from_label,
    find_template_id,
)
from fillmypdf.models.template import TemplateManifest
from fillmypdf.services.template_service import TemplateService


def test_template_id_from_label_matches_importer():
    assert (
        _template_id_from_label("ctdssmap get-download-file 1980b3d04d34")
        == "ctdssmap_get-download-file_1980b3d04d34"
    )


def test_resolve_blank_pdf_from_template_label(isolated_storage):
    tid = "ctdssmap_get-download-file_1980b3d04d34"
    pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
    TemplateService().add(
        TemplateManifest(id=tid, name="ctdssmap get-download-file 1980b3d04d34"),
        pdf,
    )
    data = {
        "fingerprint": "abc123",
        "form_label": "ctdssmap get-download-file 1980b3d04d34",
    }
    assert find_template_id(data) == tid
    path = _resolve_blank_pdf("abc123", data)
    assert path is not None
    assert path.is_file()
    assert path.name in {"template.pdf", "fillable.pdf"}


def test_resolve_blank_pdf_prefers_explicit_template_id(isolated_storage):
    tid = "my_form_v1"
    pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
    TemplateService().add(TemplateManifest(id=tid, name="My Form"), pdf)
    data = {"fingerprint": "fp1", "form_label": "unrelated name", "template_id": tid}
    assert find_template_id(data) == tid
    assert _resolve_blank_pdf("fp1", data) is not None


def test_resolve_blank_pdf_missing_returns_none(isolated_storage):
    assert _resolve_blank_pdf("nope", {"form_label": "does not exist"}) is None
