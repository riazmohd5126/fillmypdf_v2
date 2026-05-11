"""Unit tests — Excel parsing for batch uploads."""
from io import BytesIO

import pytest

from fillmypdf.services.batch_fill_service import BatchFillService


def _xlsx_bytes(rows: list) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def svc():
    return BatchFillService()


class TestParseXlsx:
    def test_two_rows_simple(self, svc):
        blob = _xlsx_bytes([
            ["first_name", "last_name"],
            ["Jane", "Doe"],
            ["Ace", "V"],
        ])
        rec = svc.parse_xlsx(blob)
        assert len(rec) == 2
        assert rec[0] == {"first_name": "Jane", "last_name": "Doe"}
        assert rec[1]["last_name"] == "V"

    def test_skips_blank_rows(self, svc):
        blob = _xlsx_bytes([
            ["a", "b"],
            ["1", ""],
            [None, None],
            ["x", "y"],
        ])
        rec = svc.parse_xlsx(blob)
        assert len(rec) == 2

    def test_numeric_bool(self, svc):
        blob = _xlsx_bytes([
            ["id", "ok"],
            [42.0, True],
        ])
        rec = svc.parse_xlsx(blob)
        assert rec[0]["id"] == "42"

    def test_only_header_raises(self, svc):
        blob = _xlsx_bytes([["only_column"]])
        with pytest.raises(ValueError, match="populated"):
            svc.parse_xlsx(blob)


    def test_raise_over_500_rows(self, svc):
        rows = [["col"]] + [[str(i)] for i in range(501)]
        blob = _xlsx_bytes(rows)
        with pytest.raises(ValueError, match="Maximum 500"):
            svc.process_xlsx_batch(
                template_pdf_path="__tmp_not_read__",
                xlsx_content=blob,
                xlsx_filename="huge.xlsx",
                ai_api_key="",
                ai_base_url="",
                ai_model="",
                batch_id="bid",
            )
