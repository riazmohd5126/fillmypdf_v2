"""
CV Field Service - OpenCV-based form field detection
=====================================================
Locates form fields (text boxes, checkboxes, underline blanks) by rendering
each PDF page to an image and analysing it with OpenCV.  Fully local: no AI,
no network, and it never calls Gemini.

Output contract
---------------
`detect_fields()` returns the SAME list-of-dicts shape as
`VisionService._get_fields_with_coords`, so the rest of the pipeline
(`_extract_labels_for_fields`, the `inspect_fillable_form` row builder,
`ExtractionService`) consumes it unchanged:

    {
      "name": str,          # real AcroForm /T when matched, else "cv_p{page}_{i}"
      "type": "/Tx"|"/Btn",
      "page": int,          # 0-based
      "x0": int, "x1": int, # pdfplumber top-origin PDF points
      "x": int,             # x center (for sorting)
      "y": int,             # top
      "y_bottom": int,      # bottom
      "cv_detected": bool,  # True when OpenCV found this box
    }

Fillability
-----------
When the PDF already has AcroForm widgets, each detected box is matched to the
best-overlapping widget (IoU >= match_iou) and inherits its real /T name and
/FT type, so downstream fills still address the correct field.  Widgets that
OpenCV missed are added back so no field is lost.  Flat/scanned PDFs (no
widgets) keep synthesized names.
"""

from __future__ import annotations

from typing import Optional


class CVDependencyError(RuntimeError):
    """Raised when OpenCV / PyMuPDF are not installed."""


def _require_cv():
    """Import cv2, numpy, fitz lazily; raise a clear error if unavailable."""
    try:
        import cv2  # noqa: F401
        import numpy as np  # noqa: F401
        import fitz  # noqa: F401  (PyMuPDF)
    except Exception as exc:  # pragma: no cover - env dependent
        raise CVDependencyError(
            "The opencv field-detection engine requires 'opencv-python-headless', "
            "'numpy' and 'PyMuPDF'.  Install them (see requirements.txt) or switch "
            "FIELD_DETECTION_ENGINE to 'acroform'."
        ) from exc
    import cv2
    import numpy as np
    import fitz
    return cv2, np, fitz


def _iou(a: dict, b: dict) -> float:
    """Intersection-over-union of two boxes given as x0/x1/y(top)/y_bottom."""
    ax0, ax1 = a["x0"], a["x1"]
    ay0, ay1 = a["y"], a.get("y_bottom", a["y"])
    bx0, bx1 = b["x0"], b["x1"]
    by0, by1 = b["y"], b.get("y_bottom", b["y"])
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


class CVFieldService:
    """Detect form-field regions from rendered page images using OpenCV."""

    def __init__(
        self,
        dpi: int = 200,
        ocr_enabled: bool = True,
        match_iou: float = 0.3,
    ):
        self.dpi = dpi
        self.ocr_enabled = ocr_enabled
        self.match_iou = match_iou

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def detect_fields(
        self,
        pdf_path: str,
        acroform_fields: Optional[list[dict]] = None,
    ) -> list[dict]:
        """
        Detect field boxes on every page.  When ``acroform_fields`` is given
        (from ``VisionService._get_fields_with_coords``), detected boxes are
        bound to real widgets and any missed widgets are added back.

        Returns fields sorted top-to-bottom, left-to-right.  Returns ``[]`` if
        OpenCV finds nothing and there are no AcroForm widgets, so the caller
        can fall back.
        """
        cv2, np, fitz = _require_cv()

        scale = self.dpi / 72.0
        detected: list[dict] = []
        page_words = self._load_page_words(pdf_path)

        doc = fitz.open(pdf_path)
        try:
            for pno in range(doc.page_count):
                page = doc[pno]
                pix = page.get_pixmap(dpi=self.dpi)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n
                )
                if pix.n == 4:
                    gray = cv2.cvtColor(img, cv2.COLOR_RGBA2GRAY)
                elif pix.n == 3:
                    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
                else:
                    gray = img[:, :, 0]

                words = page_words.get(pno, [])
                boxes = self._detect_page_boxes(cv2, np, gray)
                idx = 0
                for (x, y, w, h, kind) in boxes:
                    box_pt = {
                        "x0": x / scale, "x1": (x + w) / scale,
                        "top": y / scale, "bottom": (y + h) / scale,
                    }
                    # Drop text-boxes already full of printed text: those are
                    # table cells / label regions, not empty input fields.
                    # Checkboxes and underline blanks are exempt.
                    if kind == "textbox" and words and \
                            self._text_coverage(box_pt, words) > 0.5:
                        continue
                    detected.append(
                        {
                            "name": f"cv_p{pno}_{idx}",
                            "type": "/Btn" if kind == "checkbox" else "/Tx",
                            "page": pno,
                            "x0": round(box_pt["x0"]),
                            "x1": round(box_pt["x1"]),
                            "x": round((box_pt["x0"] + box_pt["x1"]) / 2),
                            "y": round(box_pt["top"]),
                            "y_bottom": round(box_pt["bottom"]),
                            "cv_detected": True,
                        }
                    )
                    idx += 1
        finally:
            doc.close()

        if acroform_fields:
            fields = self._bind_to_widgets(detected, acroform_fields)
        else:
            fields = detected

        fields.sort(key=lambda f: (f["page"], f["y"], f["x"]))
        return fields

    # ------------------------------------------------------------------
    # Text-layer helpers (used to reject table-cell / label rectangles)
    # ------------------------------------------------------------------
    @staticmethod
    def _load_page_words(pdf_path: str) -> dict[int, list[dict]]:
        """Per-page pdfplumber words (top-origin PDF points). {} if none."""
        try:
            import pdfplumber
        except Exception:
            return {}
        out: dict[int, list[dict]] = {}
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    try:
                        out[i] = page.extract_words(
                            x_tolerance=2, y_tolerance=2, keep_blank_chars=False
                        )
                    except Exception:
                        out[i] = []
        except Exception:
            return {}
        return out

    @staticmethod
    def _text_coverage(box: dict, words: list[dict]) -> float:
        """
        Fraction of the box's WIDTH covered by printed words that vertically
        overlap it.  A high value means the rectangle is a filled table cell /
        label, not an empty input field.
        """
        bx0, bx1 = box["x0"], box["x1"]
        bw = bx1 - bx0
        if bw <= 0:
            return 0.0
        btop, bbot = box["top"], box["bottom"]
        covered = 0.0
        for w in words:
            if min(bbot, w["bottom"]) - max(btop, w["top"]) <= 0:
                continue  # no vertical overlap
            ix0, ix1 = max(bx0, w["x0"]), min(bx1, w["x1"])
            if ix1 > ix0:
                covered += ix1 - ix0
        return min(1.0, covered / bw)

    # ------------------------------------------------------------------
    # OpenCV detection
    # ------------------------------------------------------------------
    def _detect_page_boxes(self, cv2, np, gray) -> list[tuple]:
        """
        Return raw pixel boxes [(x, y, w, h, kind), ...] where kind is
        'checkbox' | 'textbox' | 'underline'.  Coordinates are in image pixels
        at self.dpi.
        """
        h_img, w_img = gray.shape[:2]
        px_per_pt = self.dpi / 72.0

        # Binary inverse: form rules/boxes are dark on light.
        thr = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
            cv2.THRESH_BINARY_INV, 15, 10,
        )

        boxes: list[tuple] = []

        # ── 1. Rectangles (text boxes + checkboxes) via contours ────────────
        contours, _ = cv2.findContours(
            thr, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
        )
        for c in contours:
            area = cv2.contourArea(c)
            if area < (4 * px_per_pt) ** 2:  # ignore tiny specks
                continue
            approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue
            x, y, w, h = cv2.boundingRect(approx)
            w_pt, h_pt = w / px_per_pt, h / px_per_pt
            if w_pt < 6 or h_pt < 6:
                continue
            # Skip page-sized frames (whole-page borders / table outlines)
            if w > 0.95 * w_img and h > 0.95 * h_img:
                continue
            aspect = w_pt / h_pt if h_pt else 999
            if 0.6 <= aspect <= 1.6 and 6 <= w_pt <= 24 and 6 <= h_pt <= 24:
                boxes.append((x, y, w, h, "checkbox"))
            elif 8 <= h_pt <= 60 and w_pt >= 30:
                boxes.append((x, y, w, h, "textbox"))

        # ── 2. Underline blanks (long thin horizontal rules) ────────────────
        horiz_len = max(20, int(40 * px_per_pt / 10))  # ~ >= 40pt long
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (horiz_len, 1))
        horiz = cv2.morphologyEx(thr, cv2.MORPH_OPEN, kernel, iterations=1)
        hcontours, _ = cv2.findContours(
            horiz, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        for c in hcontours:
            x, y, w, h = cv2.boundingRect(c)
            w_pt, h_pt = w / px_per_pt, h / px_per_pt
            if w_pt < 40 or h_pt > 3:  # long and thin only
                continue
            # Represent the blank as a short field sitting just above the rule.
            field_h = int(12 * px_per_pt)
            fy = max(0, y - field_h)
            boxes.append((x, fy, w, field_h, "underline"))

        return self._dedupe_boxes(boxes, px_per_pt)

    @staticmethod
    def _dedupe_boxes(boxes: list[tuple], px_per_pt: float) -> list[tuple]:
        """Drop near-duplicate / nested rectangles (contours often double up)."""
        kept: list[tuple] = []
        tol = 3 * px_per_pt
        for b in sorted(boxes, key=lambda t: -(t[2] * t[3])):  # largest first
            x, y, w, h, kind = b
            dup = False
            for kx, ky, kw, kh, _ in kept:
                if (
                    abs(x - kx) < tol and abs(y - ky) < tol
                    and abs(w - kw) < tol and abs(h - kh) < tol
                ):
                    dup = True
                    break
                # fully contained inside a kept box
                if kx - tol <= x and ky - tol <= y and kx + kw + tol >= x + w and ky + kh + tol >= y + h:
                    dup = True
                    break
            if not dup:
                kept.append(b)
        return kept

    # ------------------------------------------------------------------
    # OCR labels (only used when a page has no text layer)
    # ------------------------------------------------------------------
    def ocr_labels(self, pdf_path: str, fields: list[dict]) -> dict[str, str]:
        """
        For scanned pages (no text layer), OCR the page and assign each field
        the nearest printed text to its LEFT on the same row, else ABOVE.

        Returns {field_name: label}.  Silently returns {} if pytesseract /
        PyMuPDF are unavailable.  Only pages that actually contain the given
        fields are OCR'd.
        """
        if not fields:
            return {}
        try:
            import fitz
            import numpy as np
            import pytesseract
            from PIL import Image
        except Exception as exc:  # pragma: no cover - env dependent
            print(f"  cv-ocr: OCR unavailable ({exc}); skipping OCR labels")
            return {}

        scale = self.dpi / 72.0
        by_page: dict[int, list[dict]] = {}
        for f in fields:
            by_page.setdefault(f["page"], []).append(f)

        out: dict[str, str] = {}
        doc = fitz.open(pdf_path)
        try:
            for pno, page_fields in by_page.items():
                if pno >= doc.page_count:
                    continue
                pix = doc[pno].get_pixmap(dpi=self.dpi)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples) \
                    if pix.n >= 3 else Image.frombytes("L", (pix.width, pix.height), pix.samples)
                data = pytesseract.image_to_data(
                    img, output_type=pytesseract.Output.DICT
                )
                words = []
                for i in range(len(data["text"])):
                    t = (data["text"][i] or "").strip()
                    if not t:
                        continue
                    words.append({
                        "text": t,
                        "x0": data["left"][i] / scale,
                        "x1": (data["left"][i] + data["width"][i]) / scale,
                        "top": data["top"][i] / scale,
                        "bottom": (data["top"][i] + data["height"][i]) / scale,
                    })
                for f in page_fields:
                    lbl = self._nearest_ocr_label(f, words)
                    if lbl:
                        out[f["name"]] = lbl
        finally:
            doc.close()
        return out

    @staticmethod
    def _nearest_ocr_label(field: dict, words: list[dict]) -> str:
        """Pick the closest word to the left on the same row, else above."""
        fy0, fy1 = field["y"], field.get("y_bottom", field["y"])
        fx0 = field["x0"]
        # Same-row words to the left
        left = [
            w for w in words
            if w["x1"] <= fx0 + 3
            and min(fy1, w["bottom"]) - max(fy0, w["top"]) > 0  # vertical overlap
        ]
        if left:
            left.sort(key=lambda w: w["x0"])
            return " ".join(w["text"] for w in left[-4:]).strip(" :")
        # Words directly above
        above = [
            w for w in words
            if w["bottom"] <= fy0 + 2
            and w["x0"] < field["x1"] and w["x1"] > fx0
        ]
        if above:
            above.sort(key=lambda w: w["bottom"])
            row_bottom = above[-1]["bottom"]
            row = [w for w in above if abs(w["bottom"] - row_bottom) < 6]
            row.sort(key=lambda w: w["x0"])
            return " ".join(w["text"] for w in row).strip(" :")
        return ""

    # ------------------------------------------------------------------
    # AcroForm binding (keeps fills working)
    # ------------------------------------------------------------------
    def _bind_to_widgets(
        self, detected: list[dict], widgets: list[dict]
    ) -> list[dict]:
        """
        Match each detected box to the best-overlapping AcroForm widget so it
        inherits the real /T name and /FT type.  Widgets that no box matched
        are appended so no field is lost.
        """
        # Greedy one-to-one matching: rank all overlapping (box, widget) pairs by
        # IoU and assign best-first so each box binds to at most one widget and
        # each widget is claimed by at most one box (prevents duplicate names).
        pairs: list[tuple[float, int, int]] = []
        for bi, box in enumerate(detected):
            for wi, wdg in enumerate(widgets):
                if wdg["page"] != box["page"]:
                    continue
                score = _iou(box, wdg)
                if score > 0:
                    pairs.append((score, bi, wi))
        pairs.sort(key=lambda t: t[0], reverse=True)

        box_used: set[int] = set()
        wdg_to_box: dict[int, int] = {}
        for score, bi, wi in pairs:
            if bi in box_used or wi in wdg_to_box:
                continue
            if score >= self.match_iou:
                wdg_to_box[wi] = bi
                box_used.add(bi)

        result: list[dict] = []
        # One row per widget: matched -> CV geometry + real name; else widget as-is.
        for wi, wdg in enumerate(widgets):
            if wi in wdg_to_box:
                box = detected[wdg_to_box[wi]]
                merged = dict(box)
                merged["name"] = wdg["name"]
                merged["type"] = wdg.get("type", box["type"])
                merged["cv_detected"] = True
                result.append(merged)
            else:
                w = dict(wdg)
                w["cv_detected"] = False
                result.append(w)

        # CV-only boxes: keep only genuinely new regions that do not overlap ANY
        # widget (weak overlaps are table borders / duplicates near a real field).
        for bi, box in enumerate(detected):
            if bi in box_used:
                continue
            if any(
                w["page"] == box["page"] and _iou(box, w) > 0.05
                for w in widgets
            ):
                continue
            result.append(box)

        return result
