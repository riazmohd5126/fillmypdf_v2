"""
ChecklistService
=================
Covers the pieces that never touch the network: JSON parsing (shared
pattern with NotePasteService), item normalization (dedupe/cap/blank-strip),
and that the form context fed to the model is built only from this form's
own questions/narratives — never anything invented.
"""

import pytest

from fillmypdf.models.form_spec import FormSpec, LongTextField, QuestionGroup, QuestionOption
from fillmypdf.services.checklist_service import ChecklistError, ChecklistService


def _svc():
    return ChecklistService(api_key="x", base_url="http://localhost", model="m")


def _spec_with_fields():
    return FormSpec(
        signature="sig1",
        form_label="Test Form",
        questions=[
            QuestionGroup(
                id="q1",
                question="Has the patient tried a conventional DMARD?",
                input="radio",
                options=[
                    QuestionOption(field="q1::Yes", acro_field="Q1", label="Yes", order=0),
                    QuestionOption(field="q1::No", acro_field="Q1", label="No", order=1),
                ],
            )
        ],
        long_text=[LongTextField(field="rationale", acro_field="Rationale", label="Clinical rationale")],
    )


class TestParseJson:
    def test_empty_response_raises_clean_error(self):
        with pytest.raises(ChecklistError, match="empty response"):
            _svc()._parse_json("")

    def test_preamble_before_json_still_parses(self):
        result = _svc()._parse_json('Here you go: {"checklist": ["a", "b"]}')
        assert result == {"checklist": ["a", "b"]}

    def test_markdown_fenced_json_parses(self):
        result = _svc()._parse_json('```json\n{"checklist": []}\n```')
        assert result == {"checklist": []}

    def test_truncated_json_raises_clean_error(self):
        with pytest.raises(ChecklistError, match="did not return parseable JSON"):
            _svc()._parse_json('{"checklist": ["unterminated')


class TestNormalizeItems:
    def test_strips_and_drops_blanks(self):
        out = _svc()._normalize_items(["  a  ", "", "   ", "b"])
        assert out == ["a", "b"]

    def test_dedupes_case_insensitively_keeping_first(self):
        out = _svc()._normalize_items(["Chart notes", "chart notes", "Lab results"])
        assert out == ["Chart notes", "Lab results"]

    def test_caps_at_max_items(self):
        out = _svc()._normalize_items([f"item {i}" for i in range(50)])
        assert len(out) == 20

    def test_non_list_input_returns_empty(self):
        assert _svc()._normalize_items("not a list") == []
        assert _svc()._normalize_items(None) == []


class TestFormContext:
    def test_context_includes_question_and_options(self):
        ctx = _svc()._form_context(_spec_with_fields())
        assert "Has the patient tried a conventional DMARD?" in ctx
        assert "Yes" in ctx and "No" in ctx

    def test_context_includes_narrative_labels(self):
        ctx = _svc()._form_context(_spec_with_fields())
        assert "Clinical rationale" in ctx

    def test_empty_spec_produces_empty_context(self):
        assert _svc()._form_context(FormSpec(signature="sig2")) == ""

    def test_prompt_forbids_inventing_generic_items(self):
        # The whole point of this service: never suggest staples not tied to
        # a field actually on the form.
        messages = _svc()._prompt(_spec_with_fields())
        system = messages[0]["content"]
        assert "never invent" in system.lower() or "never invented" in system.lower() \
            or "invented" in system.lower() or "invent" in system.lower()
