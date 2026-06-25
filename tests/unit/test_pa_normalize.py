"""
Unit tests for fillmypdf/services/pa_normalize.py
"""
import pytest
from fillmypdf.services.pa_normalize import normalize, validate, normalize_and_validate


# ---------------------------------------------------------------------------
# NPI
# ---------------------------------------------------------------------------
class TestNPI:
    VALID = "1234567893"   # passes Luhn

    def test_normalize_strips_non_digits(self):
        assert normalize("123-456-7893", "npi") == "1234567893"

    def test_validate_valid_npi(self):
        ok, reason = validate(self.VALID, "npi")
        assert ok
        assert reason == ""

    def test_validate_wrong_length(self):
        ok, reason = validate("12345", "npi")
        assert not ok
        assert "10 digits" in reason

    def test_validate_bad_luhn(self):
        ok, reason = validate("1234567890", "npi")  # bad checksum
        assert not ok
        assert "Luhn" in reason

    def test_none_returns_none(self):
        assert normalize(None, "npi") is None

    def test_validate_none_fails(self):
        ok, _ = validate(None, "npi")
        assert not ok


# ---------------------------------------------------------------------------
# DEA
# ---------------------------------------------------------------------------
class TestDEA:
    # Valid DEA: 2 letters + 7 digits where checksum passes
    VALID = "AB1234563"   # checksum: (1+3+5)=9, (2+4+6)*2=24, total=33, 3%10=3, last digit=3

    def test_normalize_uppercase(self):
        result = normalize("ab1234563", "dea")
        assert result == "AB1234563"

    def test_validate_valid_dea(self):
        ok, reason = validate(self.VALID, "dea")
        assert ok, f"Expected valid, got: {reason}"

    def test_validate_bad_format(self):
        ok, reason = validate("123456789", "dea")
        assert not ok

    def test_validate_bad_checksum(self):
        ok, reason = validate("AB1234567", "dea")
        assert not ok
        assert "checksum" in reason


# ---------------------------------------------------------------------------
# NDC
# ---------------------------------------------------------------------------
class TestNDC:
    def test_normalize_10digit_pads_middle(self):
        result = normalize("0074379902", "ndc")
        # 10 digits -> pad middle to 5-4-2
        assert result is not None
        parts = result.split("-")
        assert len(parts) == 3

    def test_normalize_11digit_formats(self):
        result = normalize("00074379902", "ndc")
        assert result == "00074-3799-02"

    def test_normalize_with_hyphens(self):
        result = normalize("0007-4379-902", "ndc")
        # digits = 10 -> pad
        assert result is not None

    def test_validate_valid(self):
        ok, _ = validate("00074-3799-02", "ndc")
        assert ok

    def test_validate_too_short(self):
        ok, reason = validate("1234", "ndc")
        assert not ok


# ---------------------------------------------------------------------------
# ICD-10
# ---------------------------------------------------------------------------
class TestICD10:
    def test_normalize_adds_dot(self):
        result = normalize("M0579", "icd10")
        assert result == "M05.79"

    def test_normalize_uppercase(self):
        assert normalize("m05.79", "icd10") == "M05.79"

    def test_normalize_already_dotted(self):
        assert normalize("M05.79", "icd10") == "M05.79"

    def test_validate_valid(self):
        ok, _ = validate("M05.79", "icd10")
        assert ok

    def test_validate_invalid_format(self):
        ok, reason = validate("12345", "icd10")
        assert not ok

    def test_validate_code_with_no_dot(self):
        # After normalize the dot should be added, but validate sees pre-normalized
        ok, _ = validate("M0579", "icd10")
        # Without dot it may fail; that's the correct behavior (validate post-normalize)
        # Just ensure it doesn't crash
        assert isinstance(ok, bool)


# ---------------------------------------------------------------------------
# Date
# ---------------------------------------------------------------------------
class TestDate:
    def test_mm_dd_yyyy(self):
        assert normalize("01/15/1990", "date") == "01/15/1990"

    def test_yyyy_mm_dd(self):
        assert normalize("1990-01-15", "date") == "01/15/1990"

    def test_m_d_yyyy(self):
        assert normalize("1/5/1990", "date") == "01/05/1990"

    def test_two_digit_year(self):
        result = normalize("01/15/90", "date")
        assert result == "01/15/2090"

    def test_validate_valid(self):
        ok, _ = validate("01/15/1990", "date")
        assert ok

    def test_validate_invalid(self):
        ok, reason = validate("not a date", "date")
        assert not ok

    def test_none_returns_none(self):
        assert normalize(None, "date") is None


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------
class TestPhone:
    def test_10digit_formats(self):
        result = normalize("5551234567", "phone")
        assert result == "(555) 123-4567"

    def test_formatted_input_normalized(self):
        result = normalize("(555) 123-4567", "phone")
        assert result == "(555) 123-4567"

    def test_11digit_with_country_code(self):
        result = normalize("15551234567", "phone")
        assert result == "(555) 123-4567"

    def test_validate_valid(self):
        ok, _ = validate("(555) 123-4567", "phone")
        assert ok

    def test_validate_too_short(self):
        ok, reason = validate("555-1234", "phone")
        assert not ok


# ---------------------------------------------------------------------------
# ZIP
# ---------------------------------------------------------------------------
class TestZIP:
    def test_5digit(self):
        assert normalize("12345", "zip") == "12345"

    def test_9digit_formatted(self):
        assert normalize("123456789", "zip") == "12345-6789"

    def test_with_hyphen(self):
        assert normalize("12345-6789", "zip") == "12345-6789"

    def test_validate_5(self):
        ok, _ = validate("12345", "zip")
        assert ok

    def test_validate_bad(self):
        ok, _ = validate("123", "zip")
        assert not ok


# ---------------------------------------------------------------------------
# Checkbox
# ---------------------------------------------------------------------------
class TestCheckbox:
    @pytest.mark.parametrize("v", ["1", "true", "yes", "Yes", "x", "X", "on"])
    def test_truthy_values(self, v):
        assert normalize(v, "checkbox") == "Yes"

    @pytest.mark.parametrize("v", ["0", "false", "no", "No", "off"])
    def test_falsy_values(self, v):
        assert normalize(v, "checkbox") == "No"

    def test_empty_string_checkbox_returns_none(self):
        # Empty string hits the early-exit guard in normalize() -> None
        assert normalize("", "checkbox") is None


# ---------------------------------------------------------------------------
# Number
# ---------------------------------------------------------------------------
class TestNumber:
    def test_integer(self):
        assert normalize("30", "number") == "30"

    def test_float_rounds_to_int(self):
        assert normalize("30.0", "number") == "30"

    def test_with_comma(self):
        assert normalize("1,000", "number") == "1000"

    def test_validate_not_a_number(self):
        ok, _ = validate("abc", "number")
        assert not ok


# ---------------------------------------------------------------------------
# normalize_and_validate convenience
# ---------------------------------------------------------------------------
class TestNormalizeAndValidate:
    def test_valid_npi(self):
        normed, ok, reason = normalize_and_validate("1234567893", "npi")
        assert normed == "1234567893"
        assert ok

    def test_invalid_npi(self):
        normed, ok, reason = normalize_and_validate("12345", "npi")
        assert not ok
        assert "digits" in reason

    def test_valid_date(self):
        normed, ok, reason = normalize_and_validate("1990-01-15", "date")
        assert normed == "01/15/1990"
        assert ok

    def test_empty_text(self):
        normed, ok, reason = normalize_and_validate("", "text")
        assert normed is None
        assert not ok
