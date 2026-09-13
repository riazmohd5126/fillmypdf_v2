"""
Note Paste service tests
==========================
Covers the two pieces of note_paste_service.py that never touch the
network and matter most for trust in the feature's output: JSON parsing
of the model's raw response, and the deterministic verification layer
(quote-verbatim check, duration math, zero-evidence guard).
"""

import pytest

from fillmypdf.services.note_paste_service import NotePasteError, NotePasteService


def _svc():
    return NotePasteService(api_key="x", base_url="http://localhost", model="m")


class TestParseJson:
    def test_empty_response_raises_clean_error(self):
        with pytest.raises(NotePasteError, match="empty response"):
            _svc()._parse_json("")

    def test_pure_prose_no_braces_raises_with_snippet(self):
        with pytest.raises(NotePasteError, match="safety guidelines"):
            _svc()._parse_json("I cannot process this request due to safety guidelines.")

    def test_preamble_before_json_still_parses(self):
        result = _svc()._parse_json(
            'Here is the JSON: {"answer": "yes", "confidence": "high", "evidence": []}'
        )
        assert result == {"answer": "yes", "confidence": "high", "evidence": []}

    def test_truncated_json_raises_clean_error_not_raw_decode_error(self):
        # Regression: the outermost-brace fallback used to call json.loads()
        # unguarded, leaking a bare json.JSONDecodeError instead of this.
        with pytest.raises(NotePasteError, match="did not return parseable JSON"):
            _svc()._parse_json('{"answer": "yes", "evidence": [{"quote": "unterminated')

    def test_markdown_fenced_json_parses(self):
        result = _svc()._parse_json(
            '```json\n{"answer": "no", "confidence": "high", "evidence": []}\n```'
        )
        assert result == {"answer": "no", "confidence": "high", "evidence": []}


class TestVerifyQuoteMatching:
    def test_exact_quote_verifies(self):
        text = "MTX 20mg weekly x 3+ months. Inadequate response."
        parsed = {"answer": "yes", "confidence": "high", "evidence": [{"quote": text}]}
        result = _svc()._verify(parsed, text)
        assert result.evidence[0].verified is True
        assert result.all_quotes_verified is True

    def test_fabricated_quote_does_not_verify(self):
        text = "MTX 20mg weekly x 3+ months. Inadequate response."
        parsed = {
            "answer": "yes", "confidence": "high",
            "evidence": [{"quote": "this sentence is not in the source at all"}],
        }
        result = _svc()._verify(parsed, text)
        assert result.evidence[0].verified is False
        assert result.all_quotes_verified is False
        assert result.confidence == "review_required"

    def test_line_wrapped_quote_verifies_despite_newline_mismatch(self):
        # Regression: pasted chart notes commonly wrap mid-sentence. A model
        # naturally reproduces the quote with normal spacing; the old exact
        # substring check required the literal newline too and wrongly
        # flagged an accurate quote as fabricated.
        text = (
            "Dose increased to 20mg around April, though exact date\n"
            "uncertain per chart review."
        )
        quote = "Dose increased to 20mg around April, though exact date uncertain per chart review."
        parsed = {"answer": "no", "confidence": "review_required", "evidence": [{"quote": quote}]}
        result = _svc()._verify(parsed, text)
        assert result.evidence[0].verified is True
        assert result.all_quotes_verified is True


class TestVerifyDurationMath:
    def test_meets_total_but_not_optimized_dose_forces_review(self):
        text = "note"
        parsed = {
            "answer": "yes", "confidence": "high", "evidence": [{"quote": "note"}],
            "start_date": "2026-01-01", "end_date": "2026-04-20",
            "optimized_dose_start": "2026-03-15",
        }
        result = _svc()._verify(parsed, text)
        assert result.total_months is not None and result.total_months >= 3.0
        assert result.optimized_months is not None and result.optimized_months < 3.0
        assert result.confidence == "review_required"
        assert any("optimized dose" in f for f in result.review_flags)

    def test_meets_both_durations_stays_clean(self):
        text = "note"
        parsed = {
            "answer": "yes", "confidence": "high", "evidence": [{"quote": "note"}],
            "start_date": "2026-01-01", "end_date": "2026-05-01",
            "optimized_dose_start": "2026-01-01",
        }
        result = _svc()._verify(parsed, text)
        assert result.confidence == "high"
        assert result.review_flags == []


class TestVerifyZeroEvidenceGuard:
    def test_yes_with_zero_evidence_forces_review(self):
        result = _svc()._verify({"answer": "yes", "confidence": "high", "evidence": []}, "note")
        assert result.confidence == "review_required"
        assert any("no verified evidence" in f for f in result.review_flags)

    def test_not_in_notes_with_zero_evidence_stays_clean(self):
        result = _svc()._verify(
            {"answer": "not_in_notes", "confidence": "medium", "evidence": []}, "note"
        )
        assert result.confidence == "medium"
        assert result.review_flags == []
