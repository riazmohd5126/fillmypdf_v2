"""Unit tests — ExtractionService (no PDF on disk needed)."""

import unittest.mock as mock
from pathlib import Path

import pytest

from fillmypdf.services.extraction_service import ExtractionService


@pytest.fixture
def svc():
    return ExtractionService()


class TestExtractWithoutLabels:
    def test_reads_values_via_pdf_service(self, svc, monkeypatch):
        mock_pdf = mock.Mock(spec=["get_form_fields"])
        monkeypatch.setattr(svc, "_pdf", mock_pdf)
        mock_pdf.get_form_fields.return_value = {"a": "1", "b": ""}

        out = svc.extract_pdf(Path("/fake.pdf"), include_labels=False)

        assert out.fields_detected == 2
        assert {f.name for f in out.fields} == {"a", "b"}
        assert out.non_empty_fields == 1


class TestExtractIncludeLabelsMockInspect:
    def test_merges_inspect_paths(self, svc, monkeypatch):
        mock_pdf = mock.Mock(spec=["get_form_fields"])
        monkeypatch.setattr(svc, "_pdf", mock_pdf)
        mock_pdf.get_form_fields.return_value = {"f1": "x"}

        def fake_ctor(*a, **k):
            m = mock.Mock()

            def insp(_path):
                return {
                    "fields": [
                        {
                            "name": "f1",
                            "label": "Doctor",
                            "page": 1,
                            "field_type": "text",
                        }
                    ]
                }

            m.inspect_fillable_form = mock.Mock(side_effect=insp)
            return m

        monkeypatch.setattr(
            "fillmypdf.services.extraction_service.VisionService", fake_ctor
        )

        out = svc.extract_pdf(Path("/fake.pdf"), include_labels=True)
        assert len(out.fields) == 1
        assert out.fields[0].label == "Doctor"
        assert out.fields[0].value == "x"
