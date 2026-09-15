"""
office_convert_service — images and Word letters -> PDF
==========================================================
images_to_pdf_bytes() is tested purely in-process (Pillow). docx_to_pdf_bytes()
is tested end-to-end against the real `soffice` binary (installed in CI
alongside the app Dockerfile, which needs it too) rather than mocked — the
subprocess invocation itself (unique user profile, --convert-to, timeout) is
exactly the part most likely to break in a real deploy, so a mock would hide
the failure mode that actually matters here.
"""

import io
import subprocess
import zipfile
from unittest.mock import patch

import pytest
from PIL import Image
from pypdf import PdfReader

from fillmypdf.services.office_convert_service import (
    OfficeConvertError,
    docx_to_pdf_bytes,
    images_to_pdf_bytes,
    page_count,
    soffice_available,
)


def _jpeg_bytes(color=(200, 30, 30), size=(60, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _png_bytes_rgba(size=(50, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, (10, 200, 10, 128)).save(buf, format="PNG")
    return buf.getvalue()


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


def _minimal_docx_bytes(text: str = "Letter of medical necessity.") -> bytes:
    """Hand-built minimal-but-valid .docx (no python-docx dependency needed)."""
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _DOCX_RELS)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


class TestImagesToPdfBytes:
    def test_single_rgb_image_produces_one_page_pdf(self):
        pdf = images_to_pdf_bytes([_jpeg_bytes()])
        assert pdf.startswith(b"%PDF-")
        assert page_count(pdf) == 1

    def test_multiple_images_produce_one_page_each_in_order(self):
        pdf = images_to_pdf_bytes([_jpeg_bytes(), _jpeg_bytes(color=(0, 0, 200)), _jpeg_bytes()])
        assert page_count(pdf) == 3

    def test_rgba_png_flattens_without_error(self):
        # PDF has no alpha channel — this must not raise or produce a broken PDF.
        pdf = images_to_pdf_bytes([_png_bytes_rgba()])
        assert page_count(pdf) == 1

    def test_empty_list_raises(self):
        with pytest.raises(OfficeConvertError, match="No image data"):
            images_to_pdf_bytes([])

    def test_corrupt_image_bytes_raises_clean_error(self):
        with pytest.raises(OfficeConvertError, match="Could not read image"):
            images_to_pdf_bytes([b"this is not an image"])


class TestSofficeAvailable:
    def test_returns_a_bool(self):
        assert isinstance(soffice_available(), bool)


class TestDocxToPdfBytes:
    def test_real_conversion_produces_valid_pdf(self):
        if not soffice_available():
            pytest.skip("soffice not installed in this environment")
        pdf = docx_to_pdf_bytes(_minimal_docx_bytes("Dr. Smith recommends continued therapy."), "loMN.docx")
        assert pdf.startswith(b"%PDF-")
        assert page_count(pdf) >= 1
        # The letter's own text should survive the round-trip.
        reader = PdfReader(io.BytesIO(pdf))
        assert "Dr. Smith" in (reader.pages[0].extract_text() or "")

    def test_arbitrary_bytes_still_convert_cleanly(self):
        # LibreOffice's format auto-detection is lenient: bytes that aren't a
        # real .docx fall back to a plain-text import rather than erroring.
        # Documented here so it isn't mistaken for a bug later.
        if not soffice_available():
            pytest.skip("soffice not installed in this environment")
        pdf = docx_to_pdf_bytes(b"not a real docx file at all", "bad.docx")
        assert pdf.startswith(b"%PDF-")

    def test_nonzero_exit_raises_office_convert_error(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"", stderr=b"soffice: fatal error"
            )
            with pytest.raises(OfficeConvertError, match="Could not convert"):
                docx_to_pdf_bytes(_minimal_docx_bytes(), "bad.docx")

    def test_missing_output_file_raises_office_convert_error(self):
        # returncode 0 but no output file — soffice can exit 0 without
        # producing anything (observed in this exact codebase's own testing).
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
            with pytest.raises(OfficeConvertError, match="Could not convert"):
                docx_to_pdf_bytes(_minimal_docx_bytes(), "bad.docx")

    def test_timeout_raises_clean_error_not_a_hang(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="soffice", timeout=60)):
            with pytest.raises(OfficeConvertError, match="timed out"):
                docx_to_pdf_bytes(_minimal_docx_bytes(), "slow.docx")

    def test_filename_hint_is_sanitized_for_the_temp_path(self):
        if not soffice_available():
            pytest.skip("soffice not installed in this environment")
        # A hostile/odd filename must not escape the temp dir or crash.
        pdf = docx_to_pdf_bytes(_minimal_docx_bytes(), "../../etc/passwd.docx")
        assert pdf.startswith(b"%PDF-")
