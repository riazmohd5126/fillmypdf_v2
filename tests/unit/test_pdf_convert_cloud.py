"""Cloud converter client: cold-start retry, size limits, local fallback."""

from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from fillmypdf.config import settings
from fillmypdf.services.pdf_service import PDFService


def _flat_pdf(pages: int = 1) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=612, height=792)
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


def _fillable_pdf() -> bytes:
    """One blank page carrying a real AcroForm text widget."""
    w = PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    widget = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Tx"),
        NameObject("/T"): TextStringObject("patient_name"),
        NameObject("/V"): TextStringObject(""),
        NameObject("/Rect"): ArrayObject(
            [NumberObject(v) for v in (72, 700, 300, 720)]
        ),
    })
    ref = w._add_object(widget)
    page[NameObject("/Annots")] = ArrayObject([ref])
    w._root_object[NameObject("/AcroForm")] = w._add_object(DictionaryObject({
        NameObject("/Fields"): ArrayObject([ref]),
        NameObject("/DA"): TextStringObject("/Helv 0 Tf 0 g"),
    }))
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


class _Resp:
    def __init__(self, status_code, content=b"", ctype="application/pdf"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": ctype}

    @property
    def text(self):
        return self.content.decode("utf8", "replace")


@pytest.fixture
def cloud_mode(monkeypatch):
    """Point the service at a fake converter and remove the retry sleep."""
    monkeypatch.setattr(settings, "COMMONFORMS_MODE", "cloud", raising=False)
    monkeypatch.setattr(
        settings, "CONVERT_SERVICE_URL", "https://converter.test/convert", raising=False
    )
    monkeypatch.setattr(settings, "CONVERT_SERVICE_KEY", "", raising=False)
    monkeypatch.setattr(settings, "CONVERT_SERVICE_RETRIES", 3, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_: None)


def _stub_httpx(monkeypatch, responses):
    """Install an httpx.post that returns/raises each item of ``responses``."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_retries_past_cold_start_502(tmp_path, monkeypatch, cloud_mode):
    src = tmp_path / "flat.pdf"
    src.write_bytes(_flat_pdf())
    out = tmp_path / "out.pdf"

    calls = _stub_httpx(
        monkeypatch,
        [_Resp(502, b"Bad Gateway", "text/html"), _Resp(200, _fillable_pdf())],
    )
    monkeypatch.setattr(
        PDFService,
        "_convert_via_commonforms",
        lambda self, i, o: pytest.fail("local engine ran despite a usable converter"),
    )

    report = PDFService().convert_to_fillable_detailed(src, out)

    assert len(calls) == 2, "the waking instance's 502 should not be final"
    assert report["status"] == "converted"
    assert report["engine"] == "cloud"
    assert report["field_count_after"] == 1


def test_retries_are_bounded(tmp_path, monkeypatch, cloud_mode):
    src = tmp_path / "flat.pdf"
    src.write_bytes(_flat_pdf())
    out = tmp_path / "out.pdf"

    calls = _stub_httpx(monkeypatch, [_Resp(503, b"unavailable", "text/plain")])
    monkeypatch.setattr(
        PDFService, "_convert_via_commonforms", lambda self, i, o: False
    )

    report = PDFService().convert_to_fillable_detailed(src, out)

    assert len(calls) == settings.CONVERT_SERVICE_RETRIES
    assert report["status"] == "copied_as_is"


def test_4xx_is_not_retried(tmp_path, monkeypatch, cloud_mode):
    src = tmp_path / "flat.pdf"
    src.write_bytes(_flat_pdf())
    out = tmp_path / "out.pdf"

    calls = _stub_httpx(monkeypatch, [_Resp(400, b"bad request", "text/plain")])
    monkeypatch.setattr(
        PDFService, "_convert_via_commonforms", lambda self, i, o: False
    )

    PDFService().convert_to_fillable_detailed(src, out)

    assert len(calls) == 1, "a rejected upload will be rejected again"


def test_local_commonforms_runs_when_cloud_fails(tmp_path, monkeypatch, cloud_mode):
    src = tmp_path / "flat.pdf"
    src.write_bytes(_flat_pdf())
    out = tmp_path / "out.pdf"

    _stub_httpx(monkeypatch, [_Resp(503, b"down", "text/plain")])
    fillable = _fillable_pdf()

    def fake_local(self, input_path, output_path):
        output_path.write_bytes(fillable)
        return True

    monkeypatch.setattr(PDFService, "_convert_via_commonforms", fake_local)

    report = PDFService().convert_to_fillable_detailed(src, out)

    assert report["status"] == "converted"
    assert report["engine"] == "commonforms", "report the engine that actually worked"
    assert report["field_count_after"] == 1


def test_oversized_page_count_skips_the_upload(tmp_path, monkeypatch, cloud_mode):
    monkeypatch.setattr(settings, "CONVERT_SERVICE_MAX_PAGES", 2, raising=False)
    src = tmp_path / "long.pdf"
    src.write_bytes(_flat_pdf(pages=3))
    out = tmp_path / "out.pdf"

    calls = _stub_httpx(monkeypatch, [_Resp(200, _fillable_pdf())])
    monkeypatch.setattr(
        PDFService, "_convert_via_commonforms", lambda self, i, o: False
    )

    PDFService().convert_to_fillable_detailed(src, out)

    assert calls == [], "over the page limit, so never sent"
    assert "3 pages" in PDFService()._cloud_limit_error(src)


def test_oversized_bytes_skip_the_upload(tmp_path, monkeypatch, cloud_mode):
    monkeypatch.setattr(settings, "CONVERT_SERVICE_MAX_MB", 0.0001, raising=False)
    src = tmp_path / "big.pdf"
    src.write_bytes(_flat_pdf())

    reason = PDFService()._cloud_limit_error(src)

    assert "MB" in reason


def test_zero_field_response_is_not_a_conversion(tmp_path, monkeypatch, cloud_mode):
    src = tmp_path / "flat.pdf"
    src.write_bytes(_flat_pdf())
    out = tmp_path / "out.pdf"

    _stub_httpx(monkeypatch, [_Resp(200, _flat_pdf())])
    monkeypatch.setattr(
        PDFService, "_convert_via_commonforms", lambda self, i, o: False
    )

    report = PDFService().convert_to_fillable_detailed(src, out)

    assert report["status"] == "copied_as_is"
    assert report["field_count_after"] == 0


def test_boolean_wrapper_is_false_when_copied_as_is(tmp_path, monkeypatch, cloud_mode):
    src = tmp_path / "flat.pdf"
    src.write_bytes(_flat_pdf())
    out = tmp_path / "out.pdf"
    _stub_httpx(monkeypatch, [_Resp(503, b"down", "text/plain")])
    monkeypatch.setattr(
        PDFService, "_convert_via_commonforms", lambda self, i, o: False
    )

    assert PDFService().convert_to_fillable(src, out) is False


def test_missing_url_in_cloud_mode_falls_back(tmp_path, monkeypatch, cloud_mode):
    monkeypatch.setattr(settings, "CONVERT_SERVICE_URL", "", raising=False)
    src = tmp_path / "flat.pdf"
    src.write_bytes(_flat_pdf())
    out = tmp_path / "out.pdf"

    fillable = _fillable_pdf()

    def fake_local(self, input_path, output_path):
        output_path.write_bytes(fillable)
        return True

    monkeypatch.setattr(PDFService, "_convert_via_commonforms", fake_local)

    report = PDFService().convert_to_fillable_detailed(src, out)

    assert report["status"] == "converted"
    assert report["engine"] == "commonforms"
