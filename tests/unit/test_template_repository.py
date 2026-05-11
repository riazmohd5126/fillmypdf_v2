"""
Unit tests — TemplateRepository
=================================
Uses a temporary directory so the real storage is never touched.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from fillmypdf.models.template import (
    TemplateManifest,
    TemplateDrug,
    TemplatePayer,
    TemplateQuestion,
)
from fillmypdf.repositories.template_repository import TemplateRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    """TemplateRepository pointed at a fresh temp directory."""
    monkeypatch.setattr("fillmypdf.repositories.template_repository.settings",
                        type("S", (), {"STORAGE_DIR": tmp_path})())
    return TemplateRepository()


def _manifest(tid: str = "test_tmpl") -> TemplateManifest:
    return TemplateManifest(
        id=tid,
        name="Test PA Form",
        category="prior_authorization",
        specialty="gi_motility",
        drug=TemplateDrug(name="TestDrug", generic_name="testgeneric", strengths=["10mg"]),
        payer=TemplatePayer(name="Test Payer", plan_type="medicaid", state="TX"),
        indications=["ibs-c"],
        questions=[
            TemplateQuestion(key="q1", text="Is patient over 18?")
        ],
        tags=["medicaid", "gi", "texas"],
        pages=2,
    )


FAKE_PDF = b"%PDF-1.4 fake content"


# ---------------------------------------------------------------------------
# save / exists / get
# ---------------------------------------------------------------------------


class TestSaveAndGet:

    def test_save_creates_manifest_and_pdf(self, tmp_repo):
        m = _manifest()
        tmp_repo.save(m, FAKE_PDF)
        assert tmp_repo.exists("test_tmpl")
        assert tmp_repo.has_pdf("test_tmpl")

    def test_get_returns_manifest(self, tmp_repo):
        m = _manifest()
        tmp_repo.save(m, FAKE_PDF)
        got = tmp_repo.get("test_tmpl")
        assert got is not None
        assert got.id == "test_tmpl"
        assert got.drug.name == "TestDrug"

    def test_get_missing_returns_none(self, tmp_repo):
        assert tmp_repo.get("does_not_exist") is None

    def test_pdf_bytes_stored_correctly(self, tmp_repo):
        tmp_repo.save(_manifest(), FAKE_PDF)
        assert tmp_repo.get_pdf_path("test_tmpl").read_bytes() == FAKE_PDF

    def test_save_invalidates_fillable(self, tmp_repo):
        m = _manifest()
        tmp_repo.save(m, FAKE_PDF)
        # manually write a fake fillable
        fp = tmp_repo._fillable_path("test_tmpl")
        fp.write_bytes(b"fillable")
        # re-saving should remove it
        tmp_repo.save(m, FAKE_PDF)
        assert not fp.exists()


class TestFillableCache:

    def test_has_fillable_false_initially(self, tmp_repo):
        tmp_repo.save(_manifest(), FAKE_PDF)
        assert not tmp_repo.has_fillable("test_tmpl")

    def test_save_fillable_creates_file(self, tmp_repo):
        tmp_repo.save(_manifest(), FAKE_PDF)
        p = tmp_repo.save_fillable("test_tmpl", b"fillable bytes")
        assert p.exists()
        assert tmp_repo.has_fillable("test_tmpl")
        assert tmp_repo.get_fillable_path("test_tmpl").read_bytes() == b"fillable bytes"


# ---------------------------------------------------------------------------
# List / filter
# ---------------------------------------------------------------------------


class TestListAndFilter:

    def _save_linzess(self, repo):
        m = TemplateManifest(
            id="pa_linzess_test",
            name="Linzess PA",
            category="prior_authorization",
            specialty="gi_motility",
            drug=TemplateDrug(name="Linzess", generic_name="linaclotide"),
            payer=TemplatePayer(name="Molina TX", plan_type="medicaid", state="TX"),
            tags=["medicaid", "gi"],
        )
        repo.save(m, FAKE_PDF)

    def _save_botox(self, repo):
        m = TemplateManifest(
            id="pa_botox_test",
            name="Botox PA",
            category="prior_authorization",
            specialty="neurology",
            drug=TemplateDrug(name="Botox", generic_name="onabotulinumtoxinA"),
            payer=TemplatePayer(name="RI Medicaid", plan_type="medicaid", state="RI"),
            tags=["medicaid", "neurology"],
        )
        repo.save(m, FAKE_PDF)

    def test_list_all_returns_all(self, tmp_repo):
        self._save_linzess(tmp_repo)
        self._save_botox(tmp_repo)
        assert len(tmp_repo.list_all()) == 2

    def test_list_items_filter_drug(self, tmp_repo):
        self._save_linzess(tmp_repo)
        self._save_botox(tmp_repo)
        items = tmp_repo.list_items(drug="linzess")
        assert len(items) == 1
        assert items[0].id == "pa_linzess_test"

    def test_list_items_filter_specialty(self, tmp_repo):
        self._save_linzess(tmp_repo)
        self._save_botox(tmp_repo)
        items = tmp_repo.list_items(specialty="neurology")
        assert len(items) == 1
        assert items[0].id == "pa_botox_test"

    def test_list_items_filter_state(self, tmp_repo):
        self._save_linzess(tmp_repo)
        self._save_botox(tmp_repo)
        items = tmp_repo.list_items(state="TX")
        assert len(items) == 1
        assert items[0].id == "pa_linzess_test"

    def test_list_items_filter_tag(self, tmp_repo):
        self._save_linzess(tmp_repo)
        self._save_botox(tmp_repo)
        items = tmp_repo.list_items(tag="neurology")
        assert len(items) == 1

    def test_list_items_empty_repo(self, tmp_repo):
        assert tmp_repo.list_items() == []

    def test_list_items_question_count(self, tmp_repo):
        m = _manifest()
        tmp_repo.save(m, FAKE_PDF)
        items = tmp_repo.list_items()
        assert items[0].question_count == 1


# ---------------------------------------------------------------------------
# Update manifest
# ---------------------------------------------------------------------------


class TestUpdateManifest:

    def test_save_manifest_only_updates_name(self, tmp_repo):
        tmp_repo.save(_manifest(), FAKE_PDF)
        updated = _manifest()
        updated.name = "Updated Name"
        tmp_repo.save_manifest_only(updated)
        got = tmp_repo.get("test_tmpl")
        assert got.name == "Updated Name"
        # PDF unchanged
        assert tmp_repo.has_pdf("test_tmpl")

    def test_save_manifest_only_raises_if_missing(self, tmp_repo):
        with pytest.raises(FileNotFoundError):
            tmp_repo.save_manifest_only(_manifest("nonexistent"))


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:

    def test_delete_removes_directory(self, tmp_repo):
        tmp_repo.save(_manifest(), FAKE_PDF)
        assert tmp_repo.delete("test_tmpl") is True
        assert not tmp_repo.exists("test_tmpl")

    def test_delete_missing_returns_false(self, tmp_repo):
        assert tmp_repo.delete("ghost") is False

    def test_after_delete_list_is_empty(self, tmp_repo):
        tmp_repo.save(_manifest(), FAKE_PDF)
        tmp_repo.delete("test_tmpl")
        assert tmp_repo.list_items() == []
