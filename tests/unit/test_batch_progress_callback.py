"""Unit tests — optional per-record progress callback for batch JSON."""
from unittest.mock import MagicMock, patch

import pytest

from fillmypdf.services.batch_fill_service import BatchFillService

_AUTOFILL_OK = {
    "success": True,
    "fields_detected": 0,
    "fields_filled": 1,
    "avg_confidence": 0.5,
    "cache_hit": False,
    "field_labels": {},
    "mappings": {},
    "confidence": {},
    "error": None,
}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    up = tmp_path / "up"
    out = tmp_path / "out"
    up.mkdir()
    out.mkdir(parents=True)
    monkeypatch.setattr("fillmypdf.services.batch_fill_service.settings.UPLOAD_DIR", up)
    monkeypatch.setattr("fillmypdf.services.batch_fill_service.settings.OUTPUT_DIR", out)
    return up, out


def test_process_batch_json_on_record_done_ordered(dirs):
    upload_dir, _ = dirs
    tmpl = upload_dir / "t.pdf"
    tmpl.write_bytes(b"%PDF dummy")

    svc = BatchFillService()
    svc.pdf_service.convert_to_fillable = lambda **kw: True  # type: ignore[method-assign]

    fake_vs = MagicMock()
    fake_vs.return_value.autofill_pipeline.return_value = _AUTOFILL_OK

    ticks: list[tuple[int, int, int]] = []

    def cb(c: int, s: int, f: int) -> None:
        ticks.append((c, s, f))

    records = [{"x": "1"}, {"x": "2"}]

    with patch("fillmypdf.services.batch_fill_service.VisionService", fake_vs):
        svc.process_batch_json(
            template_pdf_path=tmpl,
            user_data_array=records,
            ai_api_key="k",
            ai_base_url="http://example.invalid/",
            ai_model="m",
            batch_id="cb_test_batch",
            on_record_done=cb,
        )

    assert ticks == [(1, 1, 0), (2, 2, 0)]


def test_process_batch_json_failed_record_updates_failed_count(dirs):
    upload_dir, _ = dirs
    tmpl = upload_dir / "t.pdf"
    tmpl.write_bytes(b"%PDF dummy")

    svc = BatchFillService()
    svc.pdf_service.convert_to_fillable = lambda **kw: True  # type: ignore[method-assign]

    fake_vs = MagicMock()

    def _pipe(**kwargs):
        n = getattr(_pipe, "_n", 0)
        _pipe._n = n + 1
        if n == 0:
            return _AUTOFILL_OK
        return {**_AUTOFILL_OK, "success": False, "error": "boom"}

    _pipe._n = 0
    fake_vs.return_value.autofill_pipeline.side_effect = _pipe

    ticks: list[tuple[int, int, int]] = []

    with patch("fillmypdf.services.batch_fill_service.VisionService", fake_vs):
        svc.process_batch_json(
            template_pdf_path=tmpl,
            user_data_array=[{"a": "1"}, {"a": "2"}],
            ai_api_key="k",
            ai_base_url="http://example.invalid/",
            ai_model="m",
            batch_id="cb_test_fail_batch",
            on_record_done=lambda c, s, f: ticks.append((c, s, f)),
        )

    assert ticks == [(1, 1, 0), (2, 1, 1)]
