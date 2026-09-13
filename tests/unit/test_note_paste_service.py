"""
Note Paste service tests
==========================
Covers the two pieces of note_paste_service.py that never touch the
network and matter most for trust in the feature's output: JSON parsing
of the model's raw response, and the deterministic verification layer
(quote-verbatim check, duration math, zero-evidence guard) — now applied
per drug trial, since a summary can name more than one DMARD.
"""

import pytest

from fillmypdf.services.note_paste_service import NotePasteError, NotePasteService


def _svc():
    return NotePasteService(api_key="x", base_url="http://localhost", model="m")


def _trial(drug="methotrexate", quote="MTX 20mg weekly x 3+ months. Inadequate response.", **kw):
    t = {"drug": drug, "evidence": [{"quote": quote}]}
    t.update(kw)
    return t


class TestParseJson:
    def test_empty_response_raises_clean_error(self):
        with pytest.raises(NotePasteError, match="empty response"):
            _svc()._parse_json("")

    def test_pure_prose_no_braces_raises_with_snippet(self):
        with pytest.raises(NotePasteError, match="safety guidelines"):
            _svc()._parse_json("I cannot process this request due to safety guidelines.")

    def test_preamble_before_json_still_parses(self):
        result = _svc()._parse_json(
            'Here is the JSON: {"answer": "yes", "confidence": "high", "trials": []}'
        )
        assert result == {"answer": "yes", "confidence": "high", "trials": []}

    def test_truncated_json_raises_clean_error_not_raw_decode_error(self):
        # Regression: the outermost-brace fallback used to call json.loads()
        # unguarded, leaking a bare json.JSONDecodeError instead of this.
        with pytest.raises(NotePasteError, match="did not return parseable JSON"):
            _svc()._parse_json('{"answer": "yes", "trials": [{"drug": "unterminated')

    def test_markdown_fenced_json_parses(self):
        result = _svc()._parse_json(
            '```json\n{"answer": "no", "confidence": "high", "trials": []}\n```'
        )
        assert result == {"answer": "no", "confidence": "high", "trials": []}


class TestVerifyQuoteMatching:
    def test_exact_quote_verifies(self):
        text = "MTX 20mg weekly x 3+ months. Inadequate response."
        parsed = {"answer": "yes", "confidence": "high", "trials": [_trial(quote=text)]}
        result = _svc()._verify(parsed, text)
        assert result.trials[0].evidence[0].verified is True
        assert result.trials[0].all_quotes_verified is True
        assert result.all_quotes_verified is True

    def test_fabricated_quote_does_not_verify(self):
        text = "MTX 20mg weekly x 3+ months. Inadequate response."
        parsed = {
            "answer": "yes", "confidence": "high",
            "trials": [_trial(quote="this sentence is not in the source at all")],
        }
        result = _svc()._verify(parsed, text)
        assert result.trials[0].evidence[0].verified is False
        assert result.trials[0].all_quotes_verified is False
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
        parsed = {"answer": "no", "confidence": "review_required", "trials": [_trial(quote=quote)]}
        result = _svc()._verify(parsed, text)
        assert result.trials[0].evidence[0].verified is True
        assert result.all_quotes_verified is True


class TestVerifyDurationMath:
    def test_meets_total_but_not_optimized_dose_forces_review(self):
        text = "note"
        parsed = {
            "answer": "yes", "confidence": "high",
            "trials": [_trial(
                quote="note",
                start_date="2026-01-01", end_date="2026-04-20",
                optimized_dose_start="2026-03-15",
            )],
        }
        result = _svc()._verify(parsed, text)
        trial = result.trials[0]
        assert trial.total_months is not None and trial.total_months >= 3.0
        assert trial.optimized_months is not None and trial.optimized_months < 3.0
        assert result.confidence == "review_required"
        assert any("optimized dose" in f for f in result.review_flags)

    def test_meets_both_durations_stays_clean(self):
        text = "note"
        parsed = {
            "answer": "yes", "confidence": "high",
            "trials": [_trial(
                quote="note",
                start_date="2026-01-01", end_date="2026-05-01",
                optimized_dose_start="2026-01-01",
            )],
        }
        result = _svc()._verify(parsed, text)
        assert result.confidence == "high"
        assert result.review_flags == []


class TestVerifyZeroEvidenceGuard:
    def test_yes_with_zero_evidence_forces_review(self):
        result = _svc()._verify({"answer": "yes", "confidence": "high", "trials": []}, "note")
        assert result.confidence == "review_required"
        assert any("no verified evidence" in f for f in result.review_flags)

    def test_not_in_notes_with_zero_evidence_stays_clean(self):
        result = _svc()._verify(
            {"answer": "not_in_notes", "confidence": "medium", "trials": []}, "note"
        )
        assert result.confidence == "medium"
        assert result.review_flags == []


class TestCheckTruncation:
    """Multi-drug notes push the model closer to the token ceiling — a
    partial JSON blob used to fall through to _parse_json() and surface as
    an opaque "did not return parseable JSON". finish_reason == "length"
    is caught first with a message that actually explains what happened."""

    def test_empty_content_at_length_limit_raises_before_any_content(self):
        with pytest.raises(NotePasteError, match="before any content was produced"):
            _svc()._check_truncation("", "length")

    def test_partial_content_at_length_limit_raises_partway_through(self):
        with pytest.raises(NotePasteError, match="partway through"):
            _svc()._check_truncation('{"answer": "yes", "trials": [', "length")

    def test_normal_finish_reason_does_not_raise(self):
        _svc()._check_truncation('{"answer": "yes", "trials": []}', "stop")

    def test_none_finish_reason_does_not_raise(self):
        _svc()._check_truncation('{"answer": "yes", "trials": []}', None)


class TestVerifyMultipleTrials:
    """Regression coverage for the live Test H gap: a summary naming two
    drugs where only one trial actually carried real evidence — the
    unbacked drug's claim must be caught per-drug, not hidden behind the
    other drug's verified evidence."""

    def test_two_drugs_one_unbacked_flags_only_that_drug(self):
        text = (
            "Sulfasalazine 2g/day continued 4 months, patient reports persistent "
            "morning stiffness and swollen joints, minimal benefit noted."
        )
        parsed = {
            "answer": "yes",
            "confidence": "high",
            "summary": "Patient failed both sulfasalazine and methotrexate at optimized dose.",
            "trials": [
                _trial(
                    drug="sulfasalazine",
                    quote="Sulfasalazine 2g/day continued 4 months, patient reports "
                          "persistent morning stiffness and swollen joints, minimal "
                          "benefit noted.",
                ),
                {"drug": "methotrexate", "evidence": []},
            ],
        }
        result = _svc()._verify(parsed, text)
        assert len(result.trials) == 2
        sulfa, mtx = result.trials[0], result.trials[1]

        assert sulfa.drug == "sulfasalazine"
        assert sulfa.all_quotes_verified is True

        assert mtx.drug == "methotrexate"
        assert mtx.all_quotes_verified is False

        # Overall verification must reflect the WORST trial, not the best one.
        assert result.all_quotes_verified is False
        assert result.confidence == "review_required"
        assert any("methotrexate" in f and "No verified evidence" in f for f in result.review_flags)
        # The sulfasalazine trial must not be wrongly flagged too.
        assert not any("sulfasalazine" in f and "No verified evidence" in f for f in result.review_flags)

    def test_two_drugs_both_backed_stays_clean(self):
        text = (
            "Sulfasalazine 2g/day x4 months, minimal benefit. "
            "Methotrexate 15mg SC weekly x4 months, inadequate response."
        )
        parsed = {
            "answer": "yes",
            "confidence": "high",
            "trials": [
                _trial(drug="sulfasalazine", quote="Sulfasalazine 2g/day x4 months, minimal benefit."),
                _trial(drug="methotrexate", quote="Methotrexate 15mg SC weekly x4 months, inadequate response."),
            ],
        }
        result = _svc()._verify(parsed, text)
        assert result.all_quotes_verified is True
        assert result.confidence == "high"
        assert result.review_flags == []

    def test_drug_named_with_empty_evidence_list_is_unbacked_not_verified(self):
        # A trial with an empty evidence list (drug named but nothing quoted)
        # must never read as "verified" just because there's nothing to fail.
        result = _svc()._verify(
            {"answer": "yes", "confidence": "high", "trials": [{"drug": "leflunomide", "evidence": []}]},
            "note",
        )
        assert result.trials[0].all_quotes_verified is False
        assert result.all_quotes_verified is False
        assert result.confidence == "review_required"
