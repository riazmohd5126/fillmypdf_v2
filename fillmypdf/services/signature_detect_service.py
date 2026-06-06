"""
Signature Field Detection Service
==================================
Auto-detects signature placement zones in a PDF from two sources:

1. **AcroForm** (deterministic, no AI cost) — reads /Sig widget annotations
   and text fields whose label contains "sign", "date", or similar keywords.
   Converts each widget rect to percentage coordinates.

2. **Gemini Vision** (optional, any PDF) — renders the first N pages to
   images and asks Gemini to locate signature/date labels. Reuses the
   VisionService AI client config. Falls back to this when AcroForm yields
   nothing and AI credentials are provided.

Usage:
    svc = SignatureDetectService()
    fields = svc.detect(pdf_bytes, ai_api_key="...", ai_model="gemini-2.5-flash")
    # fields: List[DetectedSignatureField]
"""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from pypdf import PdfReader
from pypdf.generic import NameObject


# Keyword lists for label-based field detection
_SIG_KEYWORDS = re.compile(
    r"\b(sign(ature|ed|ing)?|signhere|autograph)\b",
    re.IGNORECASE,
)
_DATE_KEYWORDS = re.compile(
    r"\b(date|dated|sign.?date|date.?sign)\b",
    re.IGNORECASE,
)


@dataclass
class DetectedSignatureField:
    key: str
    label: str
    page_index: int
    x_pct: float
    y_pct: float
    width_pct: float
    height_pct: float
    source: str          # "acroform" | "ai"
    confidence: float = 1.0
    description: Optional[str] = None


class SignatureDetectService:

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def detect(
        self,
        pdf_bytes: bytes,
        *,
        ai_api_key: Optional[str] = None,
        ai_base_url: Optional[str] = None,
        ai_model: str = "gemini-2.5-flash",
        max_pages_ai: int = 3,
    ) -> List[DetectedSignatureField]:
        """
        Detect signature placement zones.

        Tries AcroForm first (free, deterministic).  If that yields nothing
        AND ``ai_api_key`` is provided, falls back to Gemini vision.

        Returns a list of ``DetectedSignatureField`` in page order.
        """
        fields = self._detect_acroform(pdf_bytes)
        if not fields and ai_api_key:
            fields = self._detect_ai(
                pdf_bytes,
                api_key=ai_api_key,
                base_url=ai_base_url,
                model=ai_model,
                max_pages=max_pages_ai,
            )
        return fields

    # ------------------------------------------------------------------
    # AcroForm detection
    # ------------------------------------------------------------------

    def _detect_acroform(self, pdf_bytes: bytes) -> List[DetectedSignatureField]:
        """
        Walk each page's annotations looking for:
          - /Sig widget annotations (explicit signature fields)
          - /Tx fields whose mapped name or /TU tooltip contains sign/date keywords

        Widget rects are converted from PDF points to % of page MediaBox.
        """
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
        except Exception:
            return []

        results: List[DetectedSignatureField] = []
        seen_keys: set[str] = set()

        for page_idx, page in enumerate(reader.pages):
            mb = page.mediabox
            page_w = float(mb.width)
            page_h = float(mb.height)
            if page_w <= 0 or page_h <= 0:
                continue

            annots = page.get("/Annots")
            if annots is None:
                continue

            for annot_ref in annots:
                try:
                    annot = annot_ref.get_object() if hasattr(annot_ref, "get_object") else annot_ref
                    if not isinstance(annot, dict):
                        continue
                except Exception:
                    continue

                field_type = annot.get("/FT")
                subtype    = annot.get("/Subtype")
                if subtype != NameObject("/Widget"):
                    continue

                field_name = str(annot.get("/T", "")).strip()
                tooltip    = str(annot.get("/TU", "")).strip()
                label_text = tooltip or field_name

                is_sig = (field_type == NameObject("/Sig"))
                is_sig_labeled = (
                    _SIG_KEYWORDS.search(field_name) or
                    _SIG_KEYWORDS.search(tooltip)
                )
                is_date_labeled = (
                    _DATE_KEYWORDS.search(field_name) or
                    _DATE_KEYWORDS.search(tooltip)
                )

                if not (is_sig or is_sig_labeled or is_date_labeled):
                    continue

                rect = annot.get("/Rect")
                if not rect or len(rect) < 4:
                    continue

                try:
                    x0 = float(rect[0])
                    y0 = float(rect[1])
                    x1 = float(rect[2])
                    y1 = float(rect[3])
                except (TypeError, ValueError):
                    continue

                # Normalise so x0 < x1, y0 < y1
                if x0 > x1:
                    x0, x1 = x1, x0
                if y0 > y1:
                    y0, y1 = y1, y0

                x_pct = round((x0 / page_w) * 100, 2)
                y_pct = round((y0 / page_h) * 100, 2)
                w_pct = round(((x1 - x0) / page_w) * 100, 2)
                h_pct = round(((y1 - y0) / page_h) * 100, 2)

                # Skip degenerate boxes
                if w_pct < 0.5 or h_pct < 0.5:
                    continue

                if is_date_labeled and not is_sig and not is_sig_labeled:
                    kind = "date"
                    human_label = label_text or "Date"
                else:
                    kind = "signature"
                    human_label = label_text or "Signature"

                base_key = re.sub(r"[^a-z0-9]", "_", (field_name or kind).lower())[:40] or kind
                key = base_key
                suffix = 1
                while key in seen_keys:
                    key = f"{base_key}_{suffix}"
                    suffix += 1
                seen_keys.add(key)

                results.append(DetectedSignatureField(
                    key=key,
                    label=human_label,
                    page_index=page_idx,
                    x_pct=x_pct,
                    y_pct=y_pct,
                    width_pct=max(5.0, w_pct),
                    height_pct=max(3.0, h_pct),
                    source="acroform",
                    confidence=1.0,
                    description=f"AcroForm widget — field type {'Sig' if is_sig else 'Tx'}",
                ))

        return results

    # ------------------------------------------------------------------
    # Gemini vision detection
    # ------------------------------------------------------------------

    def _detect_ai(
        self,
        pdf_bytes: bytes,
        *,
        api_key: str,
        base_url: Optional[str],
        model: str,
        max_pages: int,
    ) -> List[DetectedSignatureField]:
        """
        Render each of the first ``max_pages`` pages to a PNG, send to Gemini
        and ask it to identify signature/date zones as percentage bounding boxes.
        """
        try:
            import fitz  # type: ignore  # PyMuPDF — optional dep
        except ImportError:
            return self._detect_ai_pypdf_render(
                pdf_bytes, api_key=api_key, base_url=base_url, model=model, max_pages=max_pages
            )

        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception:
            return []

        results: List[DetectedSignatureField] = []
        n_pages = min(len(doc), max_pages)

        for page_idx in range(n_pages):
            page = doc[page_idx]
            mat = fitz.Matrix(1.5, 1.5)
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")
            page_fields = self._ask_gemini_for_fields(
                png_bytes, page_idx=page_idx,
                api_key=api_key, base_url=base_url, model=model,
            )
            results.extend(page_fields)

        return results

    def _detect_ai_pypdf_render(
        self,
        pdf_bytes: bytes,
        *,
        api_key: str,
        base_url: Optional[str],
        model: str,
        max_pages: int,
    ) -> List[DetectedSignatureField]:
        """
        Fallback renderer using Pillow + pypdf when PyMuPDF is not installed.
        Renders each page to a PNG via the existing DPI-based rasteriser.
        """
        try:
            from ..config import settings
            from .pdf_service import render_page_to_png  # type: ignore
        except ImportError:
            return []

        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
        except Exception:
            return []

        results: List[DetectedSignatureField] = []
        for page_idx in range(min(len(reader.pages), max_pages)):
            try:
                png_bytes = render_page_to_png(pdf_bytes, page_index=page_idx, dpi=120)
            except Exception:
                continue
            page_fields = self._ask_gemini_for_fields(
                png_bytes, page_idx=page_idx,
                api_key=api_key, base_url=base_url, model=model,
            )
            results.extend(page_fields)

        return results

    def _ask_gemini_for_fields(
        self,
        png_bytes: bytes,
        *,
        page_idx: int,
        api_key: str,
        base_url: Optional[str],
        model: str,
    ) -> List[DetectedSignatureField]:
        """Call Gemini with a page image and parse bounding-box JSON."""
        from openai import OpenAI

        b64 = base64.b64encode(png_bytes).decode()
        prompt = (
            "You are analysing a PDF form page image.\n\n"
            "Identify ALL locations where someone should sign, draw a signature, "
            "write a date, or initial.\n\n"
            "For EACH such location return a JSON object with:\n"
            '  {"label": "human-readable name", "kind": "signature"|"date"|"initial",\n'
            '   "x_pct": <left edge % of image width>,\n'
            '   "y_pct": <top edge % of image height — 0 = top>,\n'
            '   "width_pct": <width %>, "height_pct": <height %>}\n\n'
            "Return a JSON array. If none found return [].\n"
            "No markdown, no explanation — JSON only."
        )
        try:
            base = base_url or "https://generativelanguage.googleapis.com/v1beta/openai/"
            client = OpenAI(api_key=api_key, base_url=base)
            resp = client.chat.completions.create(
                model=model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                temperature=0.0,
                max_tokens=1024,
            )
            raw = (resp.choices[0].message.content or "").strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:].strip()
            parsed = json.loads(raw)
        except Exception as e:
            print(f"[signature_detect] Gemini error on page {page_idx}: {e}")
            return []

        results: List[DetectedSignatureField] = []
        for i, item in enumerate(parsed if isinstance(parsed, list) else []):
            if not isinstance(item, dict):
                continue
            try:
                label = str(item.get("label", "Signature"))
                kind  = str(item.get("kind", "signature"))
                x_pct = float(item.get("x_pct", 50))
                # Gemini uses top-left origin; PDF uses bottom-left — convert
                y_pct_top = float(item.get("y_pct", 80))
                h_pct     = float(item.get("height_pct", 8))
                # Convert top-left y to bottom-left y for PDF coordinate system
                y_pct = max(0.0, 100.0 - y_pct_top - h_pct)
                w_pct = float(item.get("width_pct", 30))
                key = re.sub(r"[^a-z0-9]", "_", label.lower())[:40] or f"{kind}_{page_idx}_{i}"
                results.append(DetectedSignatureField(
                    key=key,
                    label=label,
                    page_index=page_idx,
                    x_pct=round(x_pct, 2),
                    y_pct=round(y_pct, 2),
                    width_pct=round(max(5.0, w_pct), 2),
                    height_pct=round(max(3.0, h_pct), 2),
                    source="ai",
                    confidence=0.80,
                    description=f"Gemini vision detection — kind={kind}",
                ))
            except (TypeError, ValueError):
                continue

        return results
