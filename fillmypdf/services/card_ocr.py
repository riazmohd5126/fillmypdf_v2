"""
Insurance-card OCR (Tesseract)
==============================
Reads front/back photos locally and maps words near printed labels to the
same 7 fields Card Capture already returns.  Classification is heuristic —
vision remains the fallback when name, ID, or RxBIN is missing/invalid.
"""

from __future__ import annotations

import io
import re
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageFilter, ImageOps

# Label tokens we never treat as a field value.
_SKIP = {
    "member", "subscriber", "insured", "name", "id", "group", "grp", "bin",
    "pcn", "rx", "rxbin", "rxpcn", "rxgrp", "rxgroup", "number", "no", "num",
    "card", "health", "plan", "of", "the", "and",
}

# Specific labels first so "MEMBER ID" is not read as a member name.
_FIELD_ORDER = (
    "rx_bin", "rx_pcn", "rx_group", "member_id", "group_number", "member_name",
)

_LABEL_PATTERNS: Dict[str, List[re.Pattern[str]]] = {
    "member_name": [
        re.compile(r"\b(member|subscriber|insured)\s*name\b", re.I),
        re.compile(r"\b(subscriber|insured)\b", re.I),
    ],
    "member_id": [
        re.compile(r"\b(member|subscriber)\s*id\b", re.I),
        re.compile(r"\bid\s*(#|no|number)\b", re.I),
        re.compile(r"\bid\s*#", re.I),
        re.compile(r"\bid\b", re.I),
    ],
    "group_number": [
        re.compile(r"\bgroup\s*(#|no|number)?\b", re.I),
        re.compile(r"\bgrp\s*#?\b", re.I),
    ],
    "rx_bin": [
        re.compile(r"\brx\s*bin\b", re.I),
        re.compile(r"\bbin\b", re.I),
    ],
    "rx_pcn": [
        re.compile(r"\brx\s*pcn\b", re.I),
        re.compile(r"\bpcn\b", re.I),
    ],
    "rx_group": [
        re.compile(r"\brx\s*(grp|group)\b", re.I),
    ],
}

_KNOWN_PAYERS = (
    "aetna", "cigna", "humana", "unitedhealthcare", "united health", "uhc",
    "anthem", "blue cross", "blue shield", "bcbs", "kaiser", "molina",
    "centene", "wellcare", "oscar", "ambetter", "medicaid", "medicare",
    "tricare", "caremark", "optum", "express scripts", "horizon",
)


class TesseractUnavailable(Exception):
    pass


def tesseract_available() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _open_image(image_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray)
    return gray.filter(ImageFilter.SHARPEN)


def _words(img: Image.Image) -> List[dict]:
    import pytesseract

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    w, h = img.size
    out: List[dict] = []
    for i, raw in enumerate(data["text"]):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 20:
            continue
        left, top, width, height = (
            data["left"][i], data["top"][i], data["width"][i], data["height"][i],
        )
        out.append({
            "text": text,
            "x0": left / w,
            "y0": top / h,
            "x1": (left + width) / w,
            "y1": (top + height) / h,
            "cx": (left + width / 2) / w,
            "cy": (top + height / 2) / h,
            "conf": max(0.0, min(conf / 100.0, 1.0)),
        })
    return out


def _lines(words: List[dict], y_tol: float = 0.018) -> List[List[dict]]:
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (round(w["cy"] / y_tol), w["x0"]))
    lines: List[List[dict]] = []
    current: List[dict] = [ordered[0]]
    for w in ordered[1:]:
        if abs(w["cy"] - current[-1]["cy"]) <= y_tol:
            current.append(w)
        else:
            lines.append(sorted(current, key=lambda x: x["x0"]))
            current = [w]
    lines.append(sorted(current, key=lambda x: x["x0"]))
    return lines


def _line_text(line: List[dict]) -> str:
    return " ".join(w["text"] for w in line)


def _bbox_of(words: List[dict]) -> Optional[List[float]]:
    if not words:
        return None
    return [
        min(w["x0"] for w in words),
        min(w["y0"] for w in words),
        max(w["x1"] for w in words),
        max(w["y1"] for w in words),
    ]


def _clean_value(text: str) -> str:
    text = re.sub(r"^[:#.\-\s]+", "", text or "")
    return re.sub(r"\s+", " ", text).strip(" :-#")


def _value_after_label(line_text: str, match: re.Match[str]) -> str:
    return _clean_value(line_text[match.end():])


def _value_words_after(line: List[dict], match_end_x: float) -> List[dict]:
    after = [w for w in line if w["x0"] >= match_end_x - 0.01]
    useful = []
    for w in after:
        token = re.sub(r"[^a-z0-9]", "", w["text"].lower())
        if token in _SKIP or token in {"name", "id"}:
            continue
        useful.append(w)
    return useful


def extract_fields_from_words(words: List[dict], allowed: Tuple[str, ...]) -> Dict[str, dict]:
    """Map OCR words to card fields.  Used by the service and unit tests."""
    found: Dict[str, dict] = {}
    lines = _lines(words)
    full = " ".join(_line_text(ln) for ln in lines)

    def _record(field: str, value: str, pieces: List[dict], conf_floor: float = 0.0) -> None:
        value = _clean_value(value)
        if not value or field not in allowed:
            return
        if value.lower() in _SKIP:
            return
        conf = max([p["conf"] for p in pieces], default=0.55)
        conf = max(conf, conf_floor)
        prev = found.get(field)
        if prev is None or conf > float(prev.get("confidence") or 0):
            found[field] = {
                "value": value,
                "confidence": round(min(conf, 0.92), 3),
                "bbox": _bbox_of(pieces) or [0, 0, 1, 1],
            }

    for i, line in enumerate(lines):
        text = _line_text(line)
        next_line = lines[i + 1] if i + 1 < len(lines) else []
        next_text = _line_text(next_line)

        for field in _FIELD_ORDER:
            if field not in allowed or field in found:
                continue
            for pat in _LABEL_PATTERNS.get(field, []):
                m = pat.search(text)
                if not m:
                    continue
                rest = _value_after_label(text, m)
                # Estimate the x where the label ends so we take words to its right.
                label_end_x = line[0]["x0"]
                consumed = 0
                for w in line:
                    consumed += len(w["text"]) + 1
                    label_end_x = w["x1"]
                    if consumed >= m.end():
                        break
                right = _value_words_after(line, label_end_x)
                if rest and right:
                    _record(field, " ".join(w["text"] for w in right), right)
                elif rest:
                    _record(field, rest, line)
                elif next_text and not re.match(
                    r"^(member|id|group|grp|bin|pcn|rx)\b", next_text, re.I
                ):
                    _record(field, next_text, next_line)
                break

        # "MEMBER" on its own line, name printed underneath.
        if (
            "member_name" in allowed
            and "member_name" not in found
            and re.fullmatch(r"member(\s+name)?", text, re.I)
            and next_text
            and not re.search(r"\bid\b", next_text, re.I)
        ):
            _record("member_name", next_text, next_line)

    if "rx_bin" in allowed and "rx_bin" not in found:
        for w in words:
            digits = re.sub(r"\D", "", w["text"])
            if len(digits) == 6 and digits == w["text"].strip():
                _record("rx_bin", digits, [w], conf_floor=0.6)
                break

    if "payer_name" in allowed and "payer_name" not in found:
        blob = full.lower()
        for payer in _KNOWN_PAYERS:
            if payer in blob:
                bits = [w for w in words if payer.split()[0] in w["text"].lower()]
                _record("payer_name", payer.title(), bits or words[:3], conf_floor=0.7)
                break
        if "payer_name" not in found:
            top = [
                w for w in words
                if w["cy"] < 0.22 and w["text"].isalpha()
                and w["text"].lower() not in _SKIP and len(w["text"]) > 2
            ]
            if top:
                top.sort(key=lambda w: (w["y0"], w["x0"]))
                _record("payer_name", " ".join(w["text"] for w in top[:4]), top[:4], 0.45)

    return found


def ocr_card_side(image_bytes: bytes, allowed: Tuple[str, ...]) -> Dict[str, dict]:
    if not tesseract_available():
        raise TesseractUnavailable(
            "Tesseract is not installed. On macOS: brew install tesseract. "
            "On Debian/Render: apt-get install tesseract-ocr."
        )
    img = _open_image(image_bytes)
    return extract_fields_from_words(_words(img), allowed)
