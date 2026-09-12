"""
Card Capture Service
=====================
Two insurance-card photos (front + back) → the 7 canonical insurance fields.
One vision call per side (each side carries different fields), then
deterministic validation with no model involved:

  - RxBIN must be exactly 6 digits — anything else is rejected outright.
  - RxBIN checked against a small known-BIN list (grows over time).
  - Payer name fuzzy-matched against the template library so the UI can
    offer to auto-load the right PA form.
  - Every field carries the model's own normalized bounding box so the
    caller can show the cropped pixels next to the value (catches a
    misread digit that a format check alone would miss).

Images are never written to disk here — bytes stay in memory for the one
vision call and are dropped when this function returns.
"""

from __future__ import annotations

import base64
import difflib
import json
import re
from typing import Dict, List, Optional

from openai import OpenAI

from ..models.card_capture import BBox, CARD_FIELDS, CardCaptureResponse, CardField, MatchedTemplate

# Small seed list of publicly documented pharmacy RxBINs. Not exhaustive —
# a non-match is not treated as invalid, only unconfirmed. Grow this over
# time as more cards are seen (per the build plan).
_KNOWN_RX_BINS: Dict[str, str] = {
    "610014": "Caremark / CVS Caremark",
    "004336": "Express Scripts",
    "610279": "OptumRx",
    "610011": "Optum Rx (legacy)",
    "003858": "MedImpact",
    "610591": "Humana Pharmacy Solutions",
    "020099": "Anthem / Elixir",
    "610097": "Navitus Health Solutions",
}

_FRONT_FIELDS = ("payer_name", "member_name", "member_id", "group_number")
_BACK_FIELDS = ("rx_bin", "rx_pcn", "rx_group")


class CardCaptureError(Exception):
    pass


class CardCaptureService:
    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client = OpenAI(api_key=api_key or "unused", base_url=base_url)

    # ------------------------------------------------------------------
    def _call_side(self, image_bytes: bytes, mime: str, side: str, fields: tuple) -> Dict[str, dict]:
        b64 = base64.standard_b64encode(image_bytes).decode()
        data_uri = f"data:{mime};base64,{b64}"

        field_list = ", ".join(fields)
        prompt = (
            f"This is the {side} of a patient's health insurance card. "
            f"Look for ONLY these fields: {field_list}. If a field is not "
            "visible on this side, omit it entirely — do not guess.\n\n"
            "For each field you find, return an object with:\n"
            '  - "value": the printed text, read verbatim (preserve digits '
            "exactly — do not 'correct' anything that looks like a typo)\n"
            '  - "confidence": a number 0.0-1.0 for how legible/certain the read is\n'
            '  - "bbox": [x0, y0, x1, y1] normalized 0-1 relative to this image\'s '
            "width/height, tightly around the printed text\n\n"
            "Respond with STRICT JSON only, no markdown fences:\n"
            '{"payer_name": {"value": "...", "confidence": 0.9, "bbox": [0.1,0.05,0.6,0.15]}}'
        )
        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            max_tokens=1200,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Return strict JSON only. No markdown."},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lstrip().lower().startswith("json"):
                raw = raw.lstrip()[4:]
        raw = raw.strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            start, end = raw.find("{"), raw.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return {}
            try:
                parsed = json.loads(raw[start : end + 1])
            except Exception:
                return {}
        return parsed if isinstance(parsed, dict) else {}

    # ------------------------------------------------------------------
    @staticmethod
    def _validate_rx_bin(value: str) -> tuple[bool, Optional[str], Optional[str]]:
        """Returns (valid, reason, known_payer_match)."""
        digits = re.sub(r"\D", "", value or "")
        if len(digits) != 6 or digits != (value or "").strip():
            return False, "RxBIN must be exactly 6 digits", None
        return True, None, _KNOWN_RX_BINS.get(digits)

    def _match_templates(self, payer_name: str) -> List[MatchedTemplate]:
        if not payer_name or not payer_name.strip():
            return []
        try:
            from ..services.template_service import TemplateService

            items = TemplateService().list()
        except Exception:
            return []

        needle = payer_name.strip().lower()
        scored: List[MatchedTemplate] = []
        for item in items:
            hay = (item.payer_name or "").strip().lower()
            if not hay:
                continue
            score = difflib.SequenceMatcher(None, needle, hay).ratio()
            # Boost an exact substring match either direction (common case:
            # card says "Aetna Better Health", template says "Aetna").
            if needle in hay or hay in needle:
                score = max(score, 0.85)
            if score >= 0.55:
                scored.append(
                    MatchedTemplate(
                        template_id=item.id, name=item.name, payer_name=item.payer_name, score=round(score, 3)
                    )
                )
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:5]

    # ------------------------------------------------------------------
    def extract(
        self,
        *,
        front_bytes: bytes,
        front_mime: str,
        back_bytes: bytes,
        back_mime: str,
    ) -> CardCaptureResponse:
        warnings: List[str] = []
        raw_front = self._call_side(front_bytes, front_mime, "front", _FRONT_FIELDS)
        raw_back = self._call_side(back_bytes, back_mime, "back", _BACK_FIELDS)

        merged: Dict[str, CardField] = {}

        def _ingest(raw: dict, side: str, allowed: tuple):
            for field, info in (raw or {}).items():
                if field not in CARD_FIELDS or field not in allowed:
                    continue
                if not isinstance(info, dict):
                    continue
                value = str(info.get("value") or "").strip()
                if not value:
                    continue
                try:
                    confidence = float(info.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                bbox = None
                raw_bbox = info.get("bbox")
                if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
                    try:
                        bbox = BBox(
                            x0=float(raw_bbox[0]),
                            y0=float(raw_bbox[1]),
                            x1=float(raw_bbox[2]),
                            y1=float(raw_bbox[3]),
                        )
                    except (TypeError, ValueError):
                        bbox = None

                existing = merged.get(field)
                if existing is None or confidence > existing.confidence:
                    merged[field] = CardField(
                        field=field, value=value, confidence=confidence, side=side, bbox=bbox
                    )

        _ingest(raw_front, "front", _FRONT_FIELDS)
        _ingest(raw_back, "back", _BACK_FIELDS)

        for field in CARD_FIELDS:
            if field not in merged:
                merged[field] = CardField(field=field, value=None, confidence=0.0)

        # Deterministic validation.
        rx_bin_field = merged.get("rx_bin")
        if rx_bin_field and rx_bin_field.value:
            valid, reason, known = self._validate_rx_bin(rx_bin_field.value)
            rx_bin_field.valid = valid
            rx_bin_field.validation_reason = reason
            rx_bin_field.known_bin_match = known
            if not valid:
                warnings.append(f"RxBIN '{rx_bin_field.value}' {reason.lower()}.")

        payer_field = merged.get("payer_name")
        matched = self._match_templates(payer_field.value if payer_field else "")
        if payer_field and payer_field.value and not matched:
            warnings.append(
                f"No mapped PA form found for payer '{payer_field.value}' yet."
            )

        return CardCaptureResponse(
            fields=[merged[f] for f in CARD_FIELDS],
            matched_templates=matched,
            warnings=warnings,
        )
