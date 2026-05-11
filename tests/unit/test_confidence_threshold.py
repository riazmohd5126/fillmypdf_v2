"""
Layer 3 – confidence threshold tests
=====================================
Unit tests that verify the FILL_CONFIDENCE_THRESHOLD setting correctly
prevents low-confidence fields from being written to the output PDF.
"""
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vision_service():
    """Return a VisionService instance without hitting the network."""
    from fillmypdf.services.vision_service import VisionService
    return VisionService(api_key="test", base_url="https://fake", model="fake")


def _call_pipeline_with_threshold(threshold: float, confidence_map: dict):
    """
    Call autofill_pipeline with mocked internals so we can test the
    confidence-threshold filtering without a real PDF or AI call.
    """
    vs = _make_vision_service()

    # Fake field discovery
    fields_info = [{"name": k, "type": "text", "page": 1, "x0": 0, "y": 0, "x1": 10}
                   for k in confidence_map]
    field_labels = {k: f"Label {k}" for k in confidence_map}
    # All AI values are "VALUE_<field>"
    field_values = {k: f"VALUE_{k}" for k in confidence_map}

    with patch.object(vs, "_get_fields_with_coords", return_value=fields_info), \
         patch.object(vs, "_extract_labels_for_fields", return_value=field_labels), \
         patch.object(vs, "_map_fields_with_ai",
                      return_value=(field_values, confidence_map, False)), \
         patch.object(vs, "_fill_pdf", return_value=True), \
         patch("fillmypdf.services.vision_service.settings",
               FILL_CONFIDENCE_THRESHOLD=threshold):
        result = vs.autofill_pipeline("fake.pdf", "out.pdf", {"first_name": "Jane"})

    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConfidenceThresholdZero:
    """threshold=0.0 means write everything (backward-compatible default)."""

    def test_all_fields_written_at_zero(self):
        conf = {"field_a": 0.9, "field_b": 0.3, "field_c": 0.01}
        result = _call_pipeline_with_threshold(0.0, conf)
        assert result["fields_filled"] == 3

    def test_skipped_count_is_zero(self):
        conf = {"field_a": 0.9, "field_b": 0.1}
        result = _call_pipeline_with_threshold(0.0, conf)
        assert result["fields_skipped_low_confidence"] == 0

    def test_threshold_used_reported(self):
        result = _call_pipeline_with_threshold(0.0, {"f": 0.5})
        assert result["confidence_threshold_used"] == 0.0


class TestConfidenceThresholdNonZero:

    def test_low_confidence_field_skipped(self):
        # field_b has confidence 0.3, below threshold 0.5
        conf = {"field_a": 0.9, "field_b": 0.3}
        result = _call_pipeline_with_threshold(0.5, conf)
        assert "field_b" not in result["mappings"]
        assert "field_a" in result["mappings"]

    def test_skipped_count_matches(self):
        conf = {"a": 0.9, "b": 0.4, "c": 0.1}
        result = _call_pipeline_with_threshold(0.5, conf)
        assert result["fields_skipped_low_confidence"] == 2
        assert result["fields_filled"] == 1

    def test_exact_threshold_boundary_included(self):
        """A field exactly at the threshold should be kept (>=)."""
        conf = {"exactly": 0.5}
        result = _call_pipeline_with_threshold(0.5, conf)
        assert "exactly" in result["mappings"]

    def test_just_below_threshold_excluded(self):
        conf = {"barely": 0.499}
        result = _call_pipeline_with_threshold(0.5, conf)
        assert "barely" not in result["mappings"]

    def test_all_skipped_when_threshold_is_1(self):
        conf = {"a": 0.99, "b": 0.5}
        result = _call_pipeline_with_threshold(1.0, conf)
        assert result["fields_filled"] == 0
        assert result["fields_skipped_low_confidence"] == 2

    def test_high_threshold_preserves_perfect_confidence(self):
        conf = {"perfect": 1.0, "good": 0.7}
        result = _call_pipeline_with_threshold(1.0, conf)
        assert "perfect" in result["mappings"]
        assert result["fields_filled"] == 1

    def test_threshold_used_is_reported_in_result(self):
        result = _call_pipeline_with_threshold(0.75, {"f": 0.9})
        assert result["confidence_threshold_used"] == 0.75


class TestConfidenceThresholdMissingConfidence:
    """Fields with no confidence entry default to 1.0 (assume certain)."""

    def test_missing_confidence_passes_any_threshold(self):
        """If AI didn't return confidence for a field, treat it as 1.0."""
        vs = _make_vision_service()
        fields_info = [{"name": "no_conf_field", "type": "text",
                        "page": 1, "x0": 0, "y": 0, "x1": 10}]
        field_labels = {"no_conf_field": "Some Label"}
        field_values = {"no_conf_field": "val"}
        confidence_map = {}   # <-- empty: no confidence for this field

        with (patch.object(vs, "_get_fields_with_coords", return_value=fields_info),
              patch.object(vs, "_extract_labels_for_fields", return_value=field_labels),
              patch.object(vs, "_map_fields_with_ai",
                           return_value=(field_values, confidence_map, False)),
              patch.object(vs, "_fill_pdf", return_value=True),
              patch("fillmypdf.services.vision_service.settings",
                    FILL_CONFIDENCE_THRESHOLD=0.9)):
            result = vs.autofill_pipeline("fake.pdf", "out.pdf", {})

        assert "no_conf_field" in result["mappings"]
