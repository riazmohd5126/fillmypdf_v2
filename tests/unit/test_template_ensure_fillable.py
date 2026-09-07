"""A field-less conversion must not be cached as fillable."""

from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from fillmypdf.models.template import TemplateManifest
from fillmypdf.services.template_service import TemplateService


def _flat_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


def _fillable_pdf() -> bytes:
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


def _add(tid: str, pdf: bytes) -> TemplateService:
    svc = TemplateService()
    svc.add(TemplateManifest(id=tid, name=tid), pdf)
    return svc


def test_zero_field_conversion_is_not_cached(isolated_storage, monkeypatch):
    tid = "priv_flat_no_cache"
    svc = _add(tid, _flat_pdf())

    def fake_report(input_path, output_path):
        Path(output_path).write_bytes(Path(input_path).read_bytes())
        return {
            "ok": True,
            "status": "copied_as_is",
            "engine": "cloud",
            "field_count_before": 0,
            "field_count_after": 0,
            "page_count": 1,
            "message": "no fields",
        }

    monkeypatch.setattr(svc.pdf_service, "convert_to_fillable_detailed", fake_report)

    try:
        svc._ensure_fillable(tid)
        assert False, "expected conversion failure"
    except RuntimeError as exc:
        assert "no fields" in str(exc)

    assert not svc.repo.has_fillable(tid)


def test_poisoned_cache_is_replaced_on_retry(isolated_storage, monkeypatch):
    tid = "priv_poisoned_cache"
    svc = _add(tid, _flat_pdf())
    svc.repo.save_fillable(tid, _flat_pdf())
    assert svc.repo.has_fillable(tid)

    fillable = _fillable_pdf()

    def fake_report(input_path, output_path):
        Path(output_path).write_bytes(fillable)
        return {
            "ok": True,
            "status": "converted",
            "engine": "cloud",
            "field_count_before": 0,
            "field_count_after": 1,
            "page_count": 1,
            "message": "ok",
        }

    monkeypatch.setattr(svc.pdf_service, "convert_to_fillable_detailed", fake_report)

    path = svc._ensure_fillable(tid)
    assert TemplateService._fillable_field_count(path) == 1


def test_cached_fillable_with_fields_is_reused(isolated_storage, monkeypatch):
    tid = "priv_good_cache"
    svc = _add(tid, _fillable_pdf())
    cached = svc.repo.save_fillable(tid, _fillable_pdf())

    def boom(input_path, output_path):
        raise AssertionError("converter should not run when a real cache exists")

    monkeypatch.setattr(svc.pdf_service, "convert_to_fillable_detailed", boom)
    assert svc._ensure_fillable(tid) == cached
