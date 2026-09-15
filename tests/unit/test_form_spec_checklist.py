"""
FormSpecCache — submission checklist
=====================================
Covers set_checklist(): the whole-list replace used by both the admin's
manual edit and the AI-suggest write-back in mapping_review_routes.py.
"""

import pytest

from fillmypdf.models.form_spec import FormSpec
from fillmypdf.services.form_spec_cache import FormSpecCache


@pytest.fixture
def cache(isolated_storage) -> FormSpecCache:
    return FormSpecCache()


@pytest.fixture
def spec() -> FormSpec:
    return FormSpec(signature="sig123", form_label="Test PA Form")


class TestSetChecklist:
    def test_set_checklist_on_missing_spec_auto_creates_bare_spec(self, cache):
        # Regression: a form's canonical field map can be built and locked
        # with no question/checkbox extraction ever having run for it. An
        # admin must still be able to hand-type a checklist for it rather
        # than being blocked on that unrelated subsystem.
        ok = cache.set_checklist("brand-new-signature", ["Chart notes"], form_label="Some Form")
        assert ok is True
        created = cache.get("brand-new-signature")
        assert created is not None
        assert created.checklist == ["Chart notes"]
        assert created.form_label == "Some Form"
        assert created.questions == []
        assert created.reviewed is False

    def test_set_checklist_with_empty_signature_returns_false(self, cache):
        assert cache.set_checklist("", ["item"]) is False

    def test_set_checklist_saves_and_round_trips(self, cache, spec):
        cache.save(spec)
        ok = cache.set_checklist(spec.signature, ["Chart notes", "Lab results"])
        assert ok is True
        reloaded = cache.get(spec.signature)
        assert reloaded.checklist == ["Chart notes", "Lab results"]

    def test_set_checklist_strips_and_drops_blank_items(self, cache, spec):
        cache.save(spec)
        cache.set_checklist(spec.signature, ["  Chart notes  ", "", "   ", "Lab results"])
        reloaded = cache.get(spec.signature)
        assert reloaded.checklist == ["Chart notes", "Lab results"]

    def test_set_checklist_empty_list_clears(self, cache, spec):
        cache.save(spec)
        cache.set_checklist(spec.signature, ["one"])
        cache.set_checklist(spec.signature, [])
        assert cache.get(spec.signature).checklist == []

    def test_set_checklist_does_not_touch_reviewed_flag(self, cache, spec):
        spec.reviewed = True
        cache.save(spec)
        cache.set_checklist(spec.signature, ["item"])
        reloaded = cache.get(spec.signature)
        assert reloaded.reviewed is True
        assert reloaded.checklist == ["item"]

    def test_default_checklist_is_empty_list(self, cache, spec):
        cache.save(spec)
        assert cache.get(spec.signature).checklist == []
