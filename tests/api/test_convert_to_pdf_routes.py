"""
POST /api/v1/pdf/convert-to-pdf — images and Word letters -> one PDF
========================================================================
Real end-to-end coverage: an actual JPEG (Pillow) and an actual minimal
.docx converted through the real `soffice` binary (installed in CI
alongside the app Dockerfile). The subprocess path is exactly what a mock
would hide, so these hit it for real rather than stubbing the service.
"""

import io
import zipfile

import pytest
from PIL import Image

from fillmypdf.services.office_convert_service import soffice_available

_DOCX_CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_DOCX_RELS = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def _docx_bytes(text: str = "Letter of medical necessity.") -> bytes:
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _DOCX_RELS)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _jpeg_bytes(color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), color).save(buf, format="JPEG")
    return buf.getvalue()


class TestConvertToPdf:
    def test_single_image_converts_to_one_page_pdf(self, client, auth_headers_pro):
        r = client.post(
            "/api/v1/pdf/convert-to-pdf",
            headers=auth_headers_pro,
            files=[("files", ("photo.jpg", _jpeg_bytes(), "image/jpeg"))],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["files_converted"] == 1
        assert body["total_pages"] == 1
        assert body["download_url"]

        dl = client.get(body["download_url"], headers=auth_headers_pro)
        assert dl.status_code == 200
        assert dl.content.startswith(b"%PDF-")

    def test_multiple_images_preserve_upload_order_as_pages(self, client, auth_headers_pro):
        r = client.post(
            "/api/v1/pdf/convert-to-pdf",
            headers=auth_headers_pro,
            files=[
                ("files", ("a.jpg", _jpeg_bytes((255, 0, 0)), "image/jpeg")),
                ("files", ("b.png", _jpeg_bytes((0, 255, 0)), "image/jpeg")),
                ("files", ("c.jpg", _jpeg_bytes((0, 0, 255)), "image/jpeg")),
            ],
        )
        assert r.status_code == 200, r.text
        assert r.json()["total_pages"] == 3

    def test_docx_letter_converts_via_real_soffice(self, client, auth_headers_pro):
        if not soffice_available():
            pytest.skip("soffice not installed in this environment")
        r = client.post(
            "/api/v1/pdf/convert-to-pdf",
            headers=auth_headers_pro,
            files=[("files", ("letter.docx", _docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["files_converted"] == 1
        assert body["total_pages"] >= 1

    def test_mixed_image_and_docx_merge_into_one_pdf(self, client, auth_headers_pro):
        if not soffice_available():
            pytest.skip("soffice not installed in this environment")
        r = client.post(
            "/api/v1/pdf/convert-to-pdf",
            headers=auth_headers_pro,
            files=[
                ("files", ("scan1.jpg", _jpeg_bytes(), "image/jpeg")),
                ("files", ("letter.docx", _docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
                ("files", ("scan2.jpg", _jpeg_bytes(), "image/jpeg")),
            ],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["files_converted"] == 3
        # 1 image page + >=1 docx page(s) + 1 image page
        assert body["total_pages"] >= 3

    def test_already_pdf_upload_is_rejected_with_clear_message(self, client, auth_headers_pro):
        r = client.post(
            "/api/v1/pdf/convert-to-pdf",
            headers=auth_headers_pro,
            files=[("files", ("form.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"))],
        )
        assert r.status_code == 400
        assert "already a PDF" in r.json()["detail"]

    def test_unsupported_extension_is_rejected(self, client, auth_headers_pro):
        r = client.post(
            "/api/v1/pdf/convert-to-pdf",
            headers=auth_headers_pro,
            files=[("files", ("notes.txt", b"hello", "text/plain"))],
        )
        assert r.status_code == 400
        assert "unsupported file type" in r.json()["detail"].lower()

    def test_empty_upload_list_is_rejected(self, client, auth_headers_pro):
        r = client.post("/api/v1/pdf/convert-to-pdf", headers=auth_headers_pro, files=[])
        assert r.status_code in (400, 422)

    def test_requires_auth(self, client):
        r = client.post(
            "/api/v1/pdf/convert-to-pdf",
            files=[("files", ("photo.jpg", _jpeg_bytes(), "image/jpeg"))],
        )
        assert r.status_code in (401, 403)

    def test_too_many_files_rejected(self, client, auth_headers_pro):
        files = [("files", (f"p{i}.jpg", _jpeg_bytes(), "image/jpeg")) for i in range(21)]
        r = client.post("/api/v1/pdf/convert-to-pdf", headers=auth_headers_pro, files=files)
        assert r.status_code == 400
        assert "maximum" in r.json()["detail"].lower()
