"""
Signature Field Detection Service
==================================
Auto-detects signature placement zones in a PDF from two sources:

1. **AcroForm** (deterministic, no AI cost) — reads ``/Sig`` widgets and text
   fields that clearly look like a *signature* (or signature-date) target.
   Ordinary date fields (DOB, start/end, DOS, etc.) are ignored so they do
   not block AI fallback or get offered as stamp targets.

2. **Gemini Vision** (optional) — when AcroForm finds no true signature
   widget, renders pages and asks Gemini to locate signature lines visually.

3. **Text + underline heuristic** (flat forms) — when AcroForm/AI find
   nothing, locate printed "Signature" labels and nearby underline strokes.

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
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from pypdf import PdfReader
from pypdf.generic import NameObject


# True signature-placement labels (not "Date of Birth", "Start Date", …).
# Avoid bare "initial" — it matches "middle initial", "Initial Request", etc.
_SIG_PLACEMENT = re.compile(
    r"("
    r"\bsignatures?\b|"
    r"\bsign\s*here\b|"
    r"\bautographs?\b|"
    r"\binitials\b|"
    r"\binitial\s*here\b|"
    r"\b(physician|practitioner|provider|patient|client|guardian|witness|authorized)"
    r".{0,24}\bsign(ature|ed)?\b|"
    r"\bsign(ature|ed)?\b.{0,24}\b(physician|practitioner|provider|patient|client)\b"
    r")",
    re.IGNORECASE,
)

# Date field next to a signature ("Date Physician Signed", "Signature Date").
_SIG_DATE = re.compile(
    r"("
    r"\bdate\b.{0,20}\b(sign(ed|ature)?|physician|practitioner)\b|"
    r"\b(sign(ed|ature)?|physician|practitioner)\b.{0,20}\bdate\b|"
    r"\bsignature\s*date\b|"
    r"\bdate\s*signed\b"
    r")",
    re.IGNORECASE,
)

# Ordinary data dates — never treat as e-sign placement targets.
_ORDINARY_DATE = re.compile(
    r"("
    r"\b(date\s*of\s*birth|dob|birth\s*date)\b|"
    r"\b(start|end|from|to|revised|requested|service|admission|discharge)\b.{0,12}\bdate\b|"
    r"\bdate\b.{0,12}\b(start|end|from|to|revised|requested|service|admission|discharge)\b|"
    r"\bdos\b|\bdates?\s*of\s*service\b|"
    r"\beffective\s*date\b|\bexpiration\s*date\b"
    r")",
    re.IGNORECASE,
)


def _classify_acroform_label(
    field_name: str,
    tooltip: str,
    *,
    is_sig_type: bool,
) -> Optional[str]:
    """Return ``signature``, ``date``, or ``None`` (ignore)."""
    if is_sig_type:
        return "signature"

    blob = f"{field_name} {tooltip}".strip()
    if not blob:
        return None

    # DOB / start-end / DOS etc. — never signature targets.
    if _ORDINARY_DATE.search(blob):
        return None

    # "Date physician signed" is a date companion — check before placement
    # patterns that also match "physician … signed".
    if _SIG_DATE.search(blob):
        return "date"

    # Signature line / sign-here style labels.
    if _SIG_PLACEMENT.search(blob):
        return "signature"

    return None


# Printed labels on flat (non-AcroForm) forms.
_FLAT_SIG_LABEL = re.compile(
    r"("
    r"\bsignatures?\b|"
    r"\bsign\s*here\b|"
    r"\bprescriber\s+signature\b|"
    r"\b(physician|practitioner|provider|patient|client|guardian|member|witness|"
    r"authorized|health\s*care)\b.{0,40}\bsign(ature|ed)?\b|"
    r"\bsign(ature|ed)?\b.{0,40}\b(physician|practitioner|provider|patient|client)\b"
    r")",
    re.IGNORECASE,
)


def _repair_jsonish(text: str) -> str:
    """Best-effort fixes for common Gemini JSON mistakes."""
    s = text.strip()
    # Drop trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # Single-quoted strings → double-quoted (skip if already valid-looking)
    if "'" in s and '"' not in s:
        s = re.sub(r"'([^']*)'", r'"\1"', s)
    elif "'" in s:
        # Keys like 'label': → "label":
        s = re.sub(r"'([A-Za-z_][A-Za-z0-9_]*)'\s*:", r'"\1":', s)
        # Values '...' → "..."
        s = re.sub(r":\s*'([^']*)'", r': "\1"', s)
    return s


def _parse_gemini_fields_json(raw: str) -> List[dict]:
    """Parse Gemini vision output into a list of field dicts.

    Tolerates markdown fences, ``{"fields":[...]}`` wrappers, trailing commas,
    and single-quoted JSON — the usual failure modes that used to yield 0 zones.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty AI response")

    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        text = text.strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    candidates = [text]
    arr = re.search(r"\[[\s\S]*\]", text)
    if arr:
        candidates.append(arr.group(0))
    obj = re.search(r"\{[\s\S]*\}", text)
    if obj:
        candidates.append(obj.group(0))

    last_err: Optional[Exception] = None
    parsed: Any = None
    for cand in candidates:
        for attempt in (cand, _repair_jsonish(cand)):
            try:
                parsed = json.loads(attempt)
                break
            except Exception as e:
                last_err = e
                parsed = None
        if parsed is not None:
            break

    if parsed is None:
        raise ValueError(f"invalid AI JSON: {last_err}")

    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    if isinstance(parsed, dict):
        for key in ("fields", "zones", "signatures", "items", "data", "results"):
            inner = parsed.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        if any(k in parsed for k in ("x_pct", "label", "kind", "width_pct")):
            return [parsed]
    return []


@dataclass
class DetectedSignatureField:
    key: str
    label: str
    page_index: int
    x_pct: float
    y_pct: float
    width_pct: float
    height_pct: float
    source: str          # "acroform" | "ai" | "heuristic"
    kind: str = "signature"  # "signature" | "date" | "initial"
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
        use_ai: bool = True,
    ) -> List[DetectedSignatureField]:
        """Detect signature placement zones (AcroForm, then AI if needed)."""
        fields, _meta = self.detect_with_meta(
            pdf_bytes,
            ai_api_key=ai_api_key,
            ai_base_url=ai_base_url,
            ai_model=ai_model,
            max_pages_ai=max_pages_ai,
            use_ai=use_ai,
        )
        return fields

    def detect_with_meta(
        self,
        pdf_bytes: bytes,
        *,
        ai_api_key: Optional[str] = None,
        ai_base_url: Optional[str] = None,
        ai_model: str = "gemini-2.5-flash",
        max_pages_ai: int = 3,
        use_ai: bool = True,
    ) -> Tuple[List[DetectedSignatureField], dict]:
        """Same as ``detect`` but also returns diagnostics for the UI/API.

        Order: AcroForm → Gemini (if enabled/keyed) → text/underline heuristic.
        Companion date fields alone do **not** skip AI.
        """
        acro = self._detect_acroform(pdf_bytes)
        sig_acro = [f for f in acro if f.kind == "signature"]
        date_acro = [f for f in acro if f.kind == "date"]
        meta: dict = {
            "acroform_count": len(acro),
            "acroform_signature_count": len(sig_acro),
            "acroform_date_count": len(date_acro),
            "ai_attempted": False,
            "ai_count": 0,
            "ai_signature_count": 0,
            "ai_available": bool((ai_api_key or "").strip()),
            "ai_skipped_reason": None,
            "ai_errors": [],
            "heuristic_count": 0,
            "heuristic_used": False,
        }

        if sig_acro:
            meta["ai_skipped_reason"] = "acroform_signature_found"
            return sig_acro + date_acro, meta

        heuristic = self._detect_text_underline(pdf_bytes)
        meta["heuristic_count"] = len(heuristic)

        ai_fields: List[DetectedSignatureField] = []
        ai_errors: List[str] = []

        if use_ai and (ai_api_key or "").strip():
            meta["ai_attempted"] = True
            ai_fields, ai_errors = self._detect_ai(
                pdf_bytes,
                api_key=ai_api_key or "",
                base_url=ai_base_url,
                model=ai_model,
                max_pages=max_pages_ai,
            )
            meta["ai_errors"] = ai_errors
            ai_sigs = [f for f in ai_fields if f.kind in ("signature", "initial")]
            ai_dates = [f for f in ai_fields if f.kind == "date"]
            meta["ai_count"] = len(ai_fields)
            meta["ai_signature_count"] = len(ai_sigs)
            if ai_sigs:
                return ai_sigs + ai_dates + date_acro, meta
            if ai_errors:
                meta["ai_skipped_reason"] = "ai_parse_error"
            else:
                meta["ai_skipped_reason"] = "ai_found_none"
        elif not use_ai:
            meta["ai_skipped_reason"] = "use_ai_false"
        else:
            meta["ai_skipped_reason"] = "no_ai_key"

        if heuristic:
            meta["heuristic_used"] = True
            return heuristic + date_acro, meta

        return [], meta

    # ------------------------------------------------------------------
    # AcroForm detection
    # ------------------------------------------------------------------

    def _detect_acroform(self, pdf_bytes: bytes) -> List[DetectedSignatureField]:
        """
        Walk each page's annotations looking for:
          - ``/Sig`` widget annotations (explicit signature fields)
          - text fields whose name/tooltip clearly means a signature line
          - companion "date signed" fields (kind=date only)

        Ordinary DOB / start-end / DOS date fields are ignored.
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
                subtype = annot.get("/Subtype")
                if subtype != NameObject("/Widget") and str(subtype) != "/Widget":
                    continue

                field_name = str(annot.get("/T", "")).strip()
                tooltip = str(annot.get("/TU", "")).strip()
                label_text = tooltip or field_name
                is_sig_type = field_type == NameObject("/Sig") or str(field_type) == "/Sig"

                kind = _classify_acroform_label(
                    field_name, tooltip, is_sig_type=is_sig_type
                )
                if kind is None:
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

                if x0 > x1:
                    x0, x1 = x1, x0
                if y0 > y1:
                    y0, y1 = y1, y0

                x_pct = round((x0 / page_w) * 100, 2)
                y_pct = round((y0 / page_h) * 100, 2)
                w_pct = round(((x1 - x0) / page_w) * 100, 2)
                h_pct = round(((y1 - y0) / page_h) * 100, 2)

                if w_pct < 0.5 or h_pct < 0.5:
                    continue

                # Tiny short boxes labeled as signature are usually date lines —
                # reclassify unless the PDF type is /Sig.
                if (
                    kind == "signature"
                    and not is_sig_type
                    and h_pct < 2.5
                    and w_pct < 45
                    and _SIG_DATE.search(f"{field_name} {tooltip}")
                ):
                    kind = "date"

                human_label = label_text or ("Signature" if kind == "signature" else "Date")
                base_key = re.sub(r"[^a-z0-9]", "_", (field_name or kind).lower())[:40] or kind
                key = base_key
                suffix = 1
                while key in seen_keys:
                    key = f"{base_key}_{suffix}"
                    suffix += 1
                seen_keys.add(key)

                # Stamp boxes need usable height; grow short signature widgets a bit.
                out_h = max(3.0, h_pct) if kind == "signature" else max(2.0, h_pct)
                out_w = max(5.0, w_pct)

                results.append(DetectedSignatureField(
                    key=key,
                    label=human_label,
                    page_index=page_idx,
                    x_pct=x_pct,
                    y_pct=y_pct,
                    width_pct=out_w,
                    height_pct=out_h,
                    source="acroform",
                    kind=kind,
                    confidence=1.0,
                    description=(
                        f"AcroForm {'/Sig' if is_sig_type else '/Tx'} — kind={kind}"
                    ),
                ))

        return results

    # ------------------------------------------------------------------
    # Flat-form text + underline heuristic
    # ------------------------------------------------------------------

    @staticmethod
    def _underscore_blank_near_label(
        page,
        *,
        tx0: float,
        ty0: float,
        tx1: float,
        ty1: float,
    ) -> Optional[Tuple[float, float, float, float]]:
        """Return ``(x0, y0, w, h)`` for underscore / ``X____`` signature blanks.

        Handles two common flat-form layouts:
        1. Same row: ``Prescriber Signature:____________``
        2. Blank **above** the caption: ``X_________`` then
           ``Prescriber or Authorized Signature`` on the next line.
        """
        label_mid_y = (ty0 + ty1) / 2.0
        best: Optional[Tuple[float, float, float, float, float]] = None  # score,x0,y0,w,h

        for w in page.get_text("words") or []:
            try:
                wx0, wy0, wx1, wy1, wtext = (
                    float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])
                )
            except (TypeError, ValueError, IndexError):
                continue
            if wtext.count("_") < 5 and wtext.count("＿") < 5:
                continue

            # Skip companion Date:____ when it is not a signature word.
            if re.search(r"^\s*date\s*:", wtext, re.I) and not re.search(r"sign", wtext, re.I):
                continue

            wmid = (wy0 + wy1) / 2.0
            same_row = abs(wmid - label_mid_y) <= 12
            # Caption-below layout: blank sits just above the label.
            above_label = (wy1 <= ty0 + 4) and (wy0 >= ty0 - 40) and (wmid < label_mid_y)
            if not (same_row or above_label):
                continue

            # Horizontal association with the label (overlap or shared left margin).
            overlaps_label = not (wx1 < tx0 - 24 or wx0 > tx1 + 80)
            shares_left = abs(wx0 - tx0) <= 30
            wide_blank = (wx1 - wx0) >= max(120.0, (tx1 - tx0) * 0.8)
            if above_label and not (overlaps_label or (shares_left and wide_blank)):
                continue
            if same_row and wx1 < tx0 - 20:
                continue

            m = re.search(r"[_＿]{5,}", wtext)
            if not m:
                continue
            n = max(len(wtext), 1)
            blank_x0 = wx0 + (m.start() / n) * (wx1 - wx0)
            blank_x1 = wx0 + (m.end() / n) * (wx1 - wx0)
            # ``X____`` / leading underscores: use full word left edge.
            if m.start() <= 2 or re.match(r"^[Xx][_＿]", wtext):
                blank_x0 = wx0
            box_h = max(12.0, min(22.0, (wy1 - wy0) * 1.35))
            box_y0 = max(0.0, wy1 - box_h + 1.0)
            box_w = max(40.0, blank_x1 - blank_x0)

            score = box_w
            if re.search(r"sign", wtext, re.I):
                score += 300
            if same_row:
                score += 200
            if above_label:
                # Prefer the classic "sign above caption" blank over right-of-label guesses.
                score += 500
                if re.match(r"^[Xx][_＿]", wtext):
                    score += 150
                if shares_left:
                    score += 80
            if best is None or score > best[0]:
                best = (score, blank_x0, box_y0, box_w, box_h)

        if best is None:
            return None
        _, x0, y0, bw, bh = best
        return (x0, y0, bw, bh)

    def _detect_text_underline(self, pdf_bytes: bytes) -> List[DetectedSignatureField]:
        """Find printed signature labels + underscore blanks / underline strokes (no AI)."""
        try:
            import fitz  # type: ignore
        except ImportError:
            return []

        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception:
            return []

        results: List[DetectedSignatureField] = []
        seen: set[str] = set()

        for page_idx, page in enumerate(doc):
            W = float(page.rect.width)
            H = float(page.rect.height)
            if W <= 0 or H <= 0:
                continue

            # Collect long near-horizontal strokes (blank lines / underlines).
            lines: List[Tuple[float, float, float]] = []  # x0, y, width
            for d in page.get_drawings():
                for item in d.get("items", []):
                    if item[0] == "l":
                        p1, p2 = item[1], item[2]
                        if abs(p1.y - p2.y) <= 2.5 and abs(p1.x - p2.x) >= 60:
                            x0 = float(min(p1.x, p2.x))
                            y = float((p1.y + p2.y) / 2)
                            lines.append((x0, y, float(abs(p1.x - p2.x))))
                    elif item[0] == "re":
                        rect = item[1]
                        if rect.width >= 60 and 0.4 <= rect.height <= 10:
                            lines.append((float(rect.x0), float(rect.y0 + rect.height / 2), float(rect.width)))

            # Walk text lines looking for signature wording.
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    text = "".join(s.get("text", "") for s in spans).strip()
                    if not text or not _FLAT_SIG_LABEL.search(text):
                        continue
                    # Skip ordinary "date of service" style noise that mentions sign rarely.
                    if _ORDINARY_DATE.search(text) and not re.search(r"\bsign", text, re.I):
                        continue

                    bb = line.get("bbox") or (0, 0, 0, 0)
                    tx0, ty0, tx1, ty1 = [float(v) for v in bb]
                    label_mid_y = (ty0 + ty1) / 2
                    desc = "Printed signature label + underline heuristic"

                    # 1) Prefer underscore / X____ blanks on the label row or just above it.
                    unders = self._underscore_blank_near_label(
                        page, tx0=tx0, ty0=ty0, tx1=tx1, ty1=ty1
                    )
                    if unders:
                        box_x0, box_y0, box_w, box_h = unders
                        # Above-caption blanks sit higher than the label midline.
                        if box_y0 + box_h < label_mid_y - 4:
                            desc = "Printed signature label + underscore blank above caption"
                        else:
                            desc = "Printed signature label + underscore blank"
                    else:
                        # 2) Vector underline on the same row (not a separator below).
                        best = None  # (score, x0, y, w)
                        for lx0, ly, lw in lines:
                            if ly < ty0 - 4 or ly > ty1 + 10:
                                continue
                            to_right = lx0 >= tx0 - 8 and lx0 <= tx1 + 40
                            same_band = abs(ly - label_mid_y) <= 12
                            if not (to_right and same_band):
                                continue
                            if lw / W >= 0.72:
                                continue
                            score = lw + 400
                            if best is None or score > best[0]:
                                best = (score, lx0, ly, lw)

                        if best:
                            _, x0, y_line, lw = best
                            box_h = max(14.0, min(28.0, (ty1 - ty0) * 1.6))
                            box_y0 = max(0.0, y_line - box_h + 2.0)
                            box_x0 = x0
                            box_w = min(lw, W - box_x0)
                        else:
                            # 3) No stroke / underscores: blank to the right of the label words.
                            # Strip trailing underscore runs from the visual right edge.
                            label_right = tx0
                            for sp in spans:
                                st = sp.get("text") or ""
                                if re.fullmatch(r"[_＿\s:]+", st):
                                    continue
                                if re.search(r"[_＿]{5,}", st):
                                    # Use start of underscore run as blank; stop label earlier.
                                    continue
                                sb = sp.get("bbox") or (0, 0, 0, 0)
                                label_right = max(label_right, float(sb[2]))
                            # If text is "Prescriber Signature:___", label ends before underscores.
                            m_all = re.search(r"[_＿]{5,}", text)
                            if m_all and label_right <= tx0 + 1:
                                # Fall back: estimate from full line fraction.
                                label_right = tx0 + (m_all.start() / max(len(text), 1)) * (tx1 - tx0)

                            gap_right = tx1 - label_right
                            if gap_right >= 60:
                                box_x0 = label_right + 2
                                box_w = min(gap_right - 4, W * 0.55)
                                box_h = max(14.0, min(22.0, (ty1 - ty0) * 1.5))
                                box_y0 = max(0.0, ty1 - box_h + 1.0)
                            elif W - tx1 >= 80:
                                box_x0 = tx1 + 6
                                box_w = min(W - box_x0 - 12, W * 0.45)
                                box_h = max(16.0, (ty1 - ty0) * 1.8)
                                box_y0 = max(0.0, ty1 - box_h + 1.0)
                            else:
                                box_x0 = tx0
                                box_w = min(W * 0.55, W - box_x0 - 20)
                                box_h = 18.0
                                box_y0 = ty1 + 1

                    # Top-origin box → bottom-left % for apply.
                    x_pct = (box_x0 / W) * 100
                    width_pct = (box_w / W) * 100
                    height_pct = (box_h / H) * 100
                    y_pct = ((H - (box_y0 + box_h)) / H) * 100

                    key = re.sub(r"[^a-z0-9]", "_", text.lower())[:40] or f"sig_{page_idx}"
                    if key in seen:
                        key = f"{key}_{page_idx}"
                    seen.add(key)

                    results.append(
                        DetectedSignatureField(
                            key=key,
                            label=text[:80],
                            page_index=page_idx,
                            x_pct=round(max(0.0, min(99.0, x_pct)), 2),
                            y_pct=round(max(0.0, min(99.0, y_pct)), 2),
                            width_pct=round(max(8.0, min(90.0, width_pct)), 2),
                            height_pct=round(max(2.2, min(12.0, height_pct)), 2),
                            source="heuristic",
                            kind="signature",
                            confidence=0.78 if unders else 0.72,
                            description=desc,
                        )
                    )

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
    ) -> Tuple[List[DetectedSignatureField], List[str]]:
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
        except Exception as e:
            return [], [f"open_pdf: {e}"]

        results: List[DetectedSignatureField] = []
        errors: List[str] = []
        n_pages = min(len(doc), max_pages)

        for page_idx in range(n_pages):
            page = doc[page_idx]
            mat = fitz.Matrix(1.5, 1.5)
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")
            page_fields, err = self._ask_gemini_for_fields(
                png_bytes, page_idx=page_idx,
                api_key=api_key, base_url=base_url, model=model,
            )
            results.extend(page_fields)
            if err:
                errors.append(err)

        return results, errors

    def _detect_ai_pypdf_render(
        self,
        pdf_bytes: bytes,
        *,
        api_key: str,
        base_url: Optional[str],
        model: str,
        max_pages: int,
    ) -> Tuple[List[DetectedSignatureField], List[str]]:
        """
        Fallback renderer using Pillow + pypdf when PyMuPDF is not installed.
        Renders each page to a PNG via the existing DPI-based rasteriser.
        """
        try:
            from .pdf_service import render_page_to_png  # type: ignore
        except ImportError:
            return [], ["render: PyMuPDF and pdf_service unavailable"]

        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
        except Exception as e:
            return [], [f"open_pdf: {e}"]

        results: List[DetectedSignatureField] = []
        errors: List[str] = []
        for page_idx in range(min(len(reader.pages), max_pages)):
            try:
                png_bytes = render_page_to_png(pdf_bytes, page_index=page_idx, dpi=120)
            except Exception as e:
                errors.append(f"page {page_idx} render: {e}")
                continue
            page_fields, err = self._ask_gemini_for_fields(
                png_bytes, page_idx=page_idx,
                api_key=api_key, base_url=base_url, model=model,
            )
            results.extend(page_fields)
            if err:
                errors.append(err)

        return results, errors

    def _ask_gemini_for_fields(
        self,
        png_bytes: bytes,
        *,
        page_idx: int,
        api_key: str,
        base_url: Optional[str],
        model: str,
    ) -> Tuple[List[DetectedSignatureField], Optional[str]]:
        """Call Gemini with a page image and parse bounding-box JSON."""
        from openai import OpenAI

        b64 = base64.b64encode(png_bytes).decode()
        prompt = (
            "You are analysing a PDF form page image.\n\n"
            "Identify ALL locations where someone should sign, draw a signature, "
            "write a date next to a signature, or initial. "
            "Include printed lines labeled Signature / Prescriber Signature / "
            "Sign here even when there is no interactive form field.\n\n"
            "For EACH such location return a JSON object with DOUBLE-QUOTED keys:\n"
            '  {"label": "human-readable name", "kind": "signature"|"date"|"initial",\n'
            '   "x_pct": <left edge % of image width>,\n'
            '   "y_pct": <top edge % of image height — 0 = top>,\n'
            '   "width_pct": <width %>, "height_pct": <height %>}\n\n'
            "Return a JSON array only, e.g. []. If none found return [].\n"
            "No markdown fences, no commentary — strict JSON array only."
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
            items = _parse_gemini_fields_json(raw)
        except Exception as e:
            msg = f"page {page_idx}: {e}"
            print(f"[signature_detect] Gemini error on {msg}")
            return [], msg

        results: List[DetectedSignatureField] = []
        for i, item in enumerate(items):
            try:
                label = str(item.get("label", "Signature"))
                kind = str(item.get("kind", "signature"))
                x_pct = float(item.get("x_pct", 50))
                # Gemini uses top-left origin; PDF uses bottom-left — convert
                y_pct_top = float(item.get("y_pct", 80))
                h_pct = float(item.get("height_pct", 8))
                y_pct = max(0.0, 100.0 - y_pct_top - h_pct)
                w_pct = float(item.get("width_pct", 30))
                key = re.sub(r"[^a-z0-9]", "_", label.lower())[:40] or f"{kind}_{page_idx}_{i}"
                kind_norm = kind.lower().strip()
                if kind_norm not in ("signature", "date", "initial"):
                    kind_norm = "signature"
                results.append(DetectedSignatureField(
                    key=key,
                    label=label,
                    page_index=page_idx,
                    x_pct=round(x_pct, 2),
                    y_pct=round(y_pct, 2),
                    width_pct=round(max(5.0, w_pct), 2),
                    height_pct=round(max(3.0, h_pct), 2),
                    source="ai",
                    kind=kind_norm,
                    confidence=0.80,
                    description=f"Gemini vision detection — kind={kind_norm}",
                ))
            except (TypeError, ValueError):
                continue

        return results, None
