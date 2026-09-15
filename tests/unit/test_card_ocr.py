"""Unit tests for insurance-card Tesseract heuristics (no Tesseract binary)."""

from fillmypdf.services.card_capture_service import CardCaptureService
from fillmypdf.services.card_ocr import extract_fields_from_words

FRONT = ("payer_name", "member_name", "member_id", "group_number")
BACK = ("rx_bin", "rx_pcn", "rx_group")


def _w(text, x0, y0, x1, y1, conf=0.9):
    return {
        "text": text,
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "cx": (x0 + x1) / 2,
        "cy": (y0 + y1) / 2,
        "conf": conf,
    }


def test_extracts_labeled_front_and_back_fields():
    front = [
        _w("AETNA", 0.10, 0.04, 0.32, 0.12),
        _w("MEMBER", 0.08, 0.30, 0.22, 0.36),
        _w("NAME", 0.23, 0.30, 0.34, 0.36),
        _w("JANE", 0.40, 0.30, 0.52, 0.36),
        _w("DOE", 0.53, 0.30, 0.64, 0.36),
        _w("MEMBER", 0.08, 0.42, 0.22, 0.48),
        _w("ID", 0.23, 0.42, 0.30, 0.48),
        _w("W123456789", 0.40, 0.42, 0.72, 0.48),
        _w("GROUP", 0.08, 0.54, 0.20, 0.60),
        _w("998877", 0.40, 0.54, 0.58, 0.60),
    ]
    back = [
        _w("RX", 0.08, 0.20, 0.16, 0.26),
        _w("BIN", 0.17, 0.20, 0.28, 0.26),
        _w("610014", 0.40, 0.20, 0.56, 0.26),
        _w("PCN", 0.08, 0.34, 0.18, 0.40),
        _w("ADV", 0.40, 0.34, 0.50, 0.40),
        _w("RX", 0.08, 0.48, 0.16, 0.54),
        _w("GRP", 0.17, 0.48, 0.28, 0.54),
        _w("RX12", 0.40, 0.48, 0.52, 0.54),
    ]

    front_fields = extract_fields_from_words(front, FRONT)
    back_fields = extract_fields_from_words(back, BACK)

    assert front_fields["payer_name"]["value"] == "Aetna"
    assert front_fields["member_name"]["value"] == "JANE DOE"
    assert front_fields["member_id"]["value"] == "W123456789"
    assert front_fields["group_number"]["value"] == "998877"
    assert back_fields["rx_bin"]["value"] == "610014"
    assert back_fields["rx_pcn"]["value"] == "ADV"
    assert back_fields["rx_group"]["value"] == "RX12"


def test_member_id_is_not_read_as_member_name():
    words = [
        _w("MEMBER", 0.08, 0.30, 0.22, 0.36),
        _w("ID", 0.23, 0.30, 0.30, 0.36),
        _w("ABC999", 0.40, 0.30, 0.58, 0.36),
    ]
    fields = extract_fields_from_words(words, FRONT)
    assert fields["member_id"]["value"] == "ABC999"
    assert "member_name" not in fields


def test_ocr_fallback_when_name_or_bin_missing():
    complete_front = {
        "member_name": {"value": "Jane Doe"},
        "member_id": {"value": "W1"},
    }
    complete_back = {"rx_bin": {"value": "610014"}}
    assert CardCaptureService._ocr_needs_fallback(complete_front, complete_back) is False
    assert CardCaptureService._ocr_needs_fallback(
        {"member_id": {"value": "W1"}}, complete_back
    ) is True
    assert CardCaptureService._ocr_needs_fallback(
        complete_front, {"rx_bin": {"value": "12"}}
    ) is True


def test_merge_keeps_ocr_and_fills_gaps():
    ocr = {"member_name": {"value": "Jane Doe", "confidence": 0.8}}
    vision = {
        "member_name": {"value": "Wrong", "confidence": 0.99},
        "member_id": {"value": "W123", "confidence": 0.9},
    }
    merged = CardCaptureService._merge_fields(ocr, vision, FRONT)
    assert merged["member_name"]["value"] == "Jane Doe"
    assert merged["member_id"]["value"] == "W123"


def test_merge_replaces_invalid_ocr_bin():
    ocr = {"rx_bin": {"value": "61O014"}}
    vision = {"rx_bin": {"value": "610014"}}
    merged = CardCaptureService._merge_fields(ocr, vision, BACK)
    assert merged["rx_bin"]["value"] == "610014"


def test_auto_skips_vision_when_ocr_is_complete(monkeypatch):
    def fake_ocr(image_bytes, allowed):
        if "member_name" in allowed:
            return {
                "payer_name": {"value": "Aetna", "confidence": 0.8, "bbox": [0, 0, 1, 1]},
                "member_name": {"value": "Jane Doe", "confidence": 0.9, "bbox": [0, 0, 1, 1]},
                "member_id": {"value": "W123", "confidence": 0.9, "bbox": [0, 0, 1, 1]},
            }
        return {"rx_bin": {"value": "610014", "confidence": 0.9, "bbox": [0, 0, 1, 1]}}

    svc = CardCaptureService(api_key="unused", base_url="http://localhost", model="x")
    monkeypatch.setattr("fillmypdf.services.card_capture_service.ocr_card_side", fake_ocr)

    def boom(*_a, **_k):
        raise AssertionError("vision should not run when OCR is complete")

    monkeypatch.setattr(svc, "_call_side", boom)
    result = svc.extract(
        front_bytes=b"front",
        front_mime="image/jpeg",
        back_bytes=b"back",
        back_mime="image/jpeg",
        engine="auto",
    )
    assert result.engine == "auto"
    assert result.engine_used == "tesseract"
    values = {f.field: f.value for f in result.fields}
    assert values["member_name"] == "Jane Doe"
    assert values["rx_bin"] == "610014"
    assert values["rx_bin"] and result.fields[4].valid is True


def test_auto_hybrid_fills_missing_id(monkeypatch):
    def fake_ocr(image_bytes, allowed):
        if "member_name" in allowed:
            return {"member_name": {"value": "Jane Doe", "confidence": 0.8, "bbox": [0, 0, 1, 1]}}
        return {"rx_bin": {"value": "610014", "confidence": 0.9, "bbox": [0, 0, 1, 1]}}

    def fake_vision(image_bytes, mime, side, fields):
        if side == "front":
            return {"member_id": {"value": "W123", "confidence": 0.95, "bbox": [0, 0, 1, 1]}}
        return {}

    svc = CardCaptureService(api_key="secret", base_url="http://localhost", model="x")
    monkeypatch.setattr("fillmypdf.services.card_capture_service.ocr_card_side", fake_ocr)
    monkeypatch.setattr(svc, "_call_side", fake_vision)
    result = svc.extract(
        front_bytes=b"front",
        front_mime="image/jpeg",
        back_bytes=b"back",
        back_mime="image/jpeg",
        engine="auto",
    )
    assert result.engine_used == "hybrid"
    values = {f.field: f.value for f in result.fields}
    assert values["member_name"] == "Jane Doe"
    assert values["member_id"] == "W123"
