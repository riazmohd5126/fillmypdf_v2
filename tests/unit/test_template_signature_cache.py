"""
Unit tests for fillmypdf.services.template_signature_cache
==========================================================
The library listing depends on these two caches staying both fast (no PDF open
per template) and correct (a freshly locked map shows up immediately).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fillmypdf.services import template_signature_cache as tsc
from fillmypdf.services.canonical_map_cache import CanonicalMapCache
from fillmypdf.services.template_access import map_index


class _StubRepo:
    def __init__(self, paths: dict):
        self.paths = paths

    def get_fillable_path(self, template_id: str):
        return self.paths.get(template_id)


class _StubService:
    """Stands in for TemplateService; records any conversion attempt."""

    def __init__(self, paths: dict):
        self.repo = _StubRepo(paths)
        self.converted: list = []

    def _ensure_fillable(self, template_id: str):
        self.converted.append(template_id)
        raise RuntimeError("no PDF to convert")


@pytest.fixture
def fake_pdf(tmp_path) -> Path:
    p = tmp_path / "one.pdf"
    p.write_bytes(b"%PDF-1.7 first")
    return p


@pytest.fixture
def counting_compute(monkeypatch):
    """Replace signature computation (which opens the PDF) with a counter."""
    calls: list = []

    def _compute(path):
        calls.append(str(path))
        return "sig-" + Path(path).stem

    monkeypatch.setattr(tsc, "_compute", _compute)
    return calls


class TestSignatureReuse:

    def test_computed_once_then_served_from_cache(
        self, isolated_storage, fake_pdf, counting_compute
    ):
        svc = _StubService({"tpl": fake_pdf})
        assert tsc.get("tpl", service=svc) == "sig-one"
        assert tsc.get("tpl", service=svc) == "sig-one"
        assert len(counting_compute) == 1

    def test_index_persisted_to_disk(
        self, isolated_storage, fake_pdf, counting_compute
    ):
        svc = _StubService({"tpl": fake_pdf})
        tsc.get("tpl", service=svc)
        index = isolated_storage / "template_signature_cache" / "index.json"
        assert index.exists()
        assert json.loads(index.read_text())["tpl"]["signature"] == "sig-one"

    def test_changed_pdf_recomputes(
        self, isolated_storage, fake_pdf, counting_compute
    ):
        svc = _StubService({"tpl": fake_pdf})
        tsc.get("tpl", service=svc)
        fake_pdf.write_bytes(b"%PDF-1.7 second, a different size")
        tsc.get("tpl", service=svc)
        assert len(counting_compute) == 2

    def test_bust_forces_recompute(
        self, isolated_storage, fake_pdf, counting_compute
    ):
        svc = _StubService({"tpl": fake_pdf})
        tsc.get("tpl", service=svc)
        tsc.bust("tpl")
        tsc.get("tpl", service=svc)
        assert len(counting_compute) == 2

    def test_widgetless_pdf_is_cached_as_none(self, isolated_storage, fake_pdf, monkeypatch):
        calls: list = []
        monkeypatch.setattr(tsc, "_compute", lambda p: calls.append(p) or None)
        svc = _StubService({"tpl": fake_pdf})
        assert tsc.get("tpl", service=svc) is None
        assert tsc.get("tpl", service=svc) is None
        assert len(calls) == 1


class TestConversionPolicy:

    def test_bulk_callers_never_trigger_conversion(
        self, isolated_storage, counting_compute
    ):
        svc = _StubService({})
        out = tsc.get_many(["a", "b"], service=svc, allow_convert=False)
        assert out == {"a": None, "b": None}
        assert svc.converted == []
        assert counting_compute == []

    def test_single_lookup_may_convert(self, isolated_storage, counting_compute):
        svc = _StubService({})
        assert tsc.get("a", service=svc) is None
        assert svc.converted == ["a"]


class TestMapIndexInvalidation:

    def test_locking_a_map_is_picked_up(self, isolated_storage):
        cache = CanonicalMapCache()
        cache.set(
            "fp1",
            {"1": {"canonical": "patient.dob", "confidence": "high", "source": "catalog"}},
            signature="sig-abc",
            form_label="Some Form",
            reviewed=False,
        )
        assert "sig-abc" not in map_index()["signatures"]

        cache.set_reviewed("fp1", True)
        assert "sig-abc" in map_index()["signatures"]

    def test_locked_entry_without_mappings_is_not_honored(self, isolated_storage):
        cache = CanonicalMapCache()
        cache.save_full(
            "fp2",
            {"signature": "sig-empty", "reviewed": True, "mappings": None},
        )
        assert "sig-empty" not in map_index()["signatures"]
