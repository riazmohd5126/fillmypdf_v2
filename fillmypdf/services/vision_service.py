"""
Vision Service - AI-powered PDF auto-fill
"""
from __future__ import annotations

import json
from typing import Dict, Optional, Tuple

from openai import OpenAI
from pypdf import PdfReader, PdfWriter

from .template_cache import MappingEntry, TemplateCache
from ..config import settings


class VisionService:
    """AI-powered PDF field mapping and filling"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._cache = TemplateCache()

    # ------------------------------------------------------------------
    # Step 1: Extract fields with full bounding box + page info
    # ------------------------------------------------------------------

    def _get_fields_with_coords(self, pdf_path: str) -> list[dict]:
        """
        Read every AcroForm annotation and return a list of dicts with:
          name, type, page, x0, x1, y (top-of-page, same system as pdfplumber)
        Sorted top-to-bottom then left-to-right.
        """
        try:
            reader = PdfReader(pdf_path)
            fields_info = []
            for page_num, page in enumerate(reader.pages):
                page_height = float(page.mediabox.height)
                if "/Annots" not in page:
                    continue
                for annot_ref in page["/Annots"]:
                    try:
                        annot = annot_ref.get_object()
                        name_obj = annot.get("/T")
                        if name_obj is None:
                            continue
                        name = str(name_obj)
                        rect = annot.get("/Rect")
                        ft_obj = annot.get("/FT")
                        ft = str(ft_obj) if ft_obj else "/Tx"
                        if rect:
                            x0 = float(rect[0])
                            y0 = float(rect[1])
                            x1 = float(rect[2])
                            y1 = float(rect[3])
                            # Convert PDF bottom-origin → top-origin (matches pdfplumber)
                            field_top = page_height - max(y0, y1)
                        else:
                            x0 = x1 = field_top = 0.0
                        fields_info.append({
                            "name": name,
                            "type": ft,
                            "page": page_num,
                            "x0": round(x0),
                            "x1": round(x1),
                            "x": round((x0 + x1) / 2),   # center (for sorting)
                            "y": round(field_top),         # distance from top
                        })
                    except Exception:
                        continue
            fields_info.sort(key=lambda f: (f["page"], f["y"], f["x"]))
            return fields_info
        except Exception as e:
            print(f"  ⚠️  Could not read PDF fields: {e}")
            return []

    # ------------------------------------------------------------------
    # Step 2: Assign a human-readable label to every field
    #         using pdfplumber word positions
    # ------------------------------------------------------------------

    def _extract_labels_for_fields(
        self, pdf_path: str, fields_info: list[dict]
    ) -> dict[str, str]:
        """
        For each field, use pdfplumber to find words on the same horizontal
        band that are immediately adjacent to it:

        - Most fields (textboxes): look LEFT  → take the 4 closest words
        - Fields at far-left (checkboxes, x0 < 80): look RIGHT → take 6 words
          (these are checkbox-per-question rows where the question is to the right)

        Falls back to raw field name if no words found.
        """
        import pdfplumber

        Y_TOLERANCE = 12      # px: field top-edge sits ~9-10px above text baseline
        MAX_LEFT_DIST  = 220  # px: don't grab labels further left than this
        MAX_RIGHT_DIST = 500  # px: for checkboxes, scan up to 500px right
        MAX_LABEL_WORDS = 4   # rightmost N words left of field (the label)
        CHECKBOX_X_THRESHOLD = 80  # fields left of this x are likely checkboxes

        field_labels: dict[str, str] = {}

        try:
            with pdfplumber.open(pdf_path) as pdf:
                page_words: dict[int, list[dict]] = {}
                for i, plumb_page in enumerate(pdf.pages):
                    page_words[i] = plumb_page.extract_words(
                        x_tolerance=3, y_tolerance=3, keep_blank_chars=False
                    )

                for field in fields_info:
                    pnum   = field["page"]
                    words  = page_words.get(pnum, [])
                    f_top  = field["y"]
                    f_x0   = field["x0"]
                    f_x1   = field["x1"]

                    is_checkbox = f_x0 < CHECKBOX_X_THRESHOLD

                    candidates = []
                    for w in words:
                        if abs(w["top"] - f_top) > Y_TOLERANCE:
                            continue

                        if is_checkbox:
                            # Look to the RIGHT of the checkbox
                            if w["x0"] < f_x1 - 5:
                                continue   # word starts before field ends
                            if w["x0"] - f_x1 > MAX_RIGHT_DIST:
                                continue
                        else:
                            # Look to the LEFT of the textbox
                            if w["x1"] > f_x0 + 5:
                                continue   # word overlaps field or is to the right
                            if f_x0 - w["x0"] > MAX_LEFT_DIST:
                                continue

                        candidates.append(w)

                    if candidates:
                        candidates.sort(key=lambda w: w["x0"])
                        if is_checkbox:
                            label_words = candidates[:6]   # leftmost 6 words after checkbox
                        else:
                            label_words = candidates[-MAX_LABEL_WORDS:]  # rightmost 4 before field
                        label = " ".join(w["text"] for w in label_words).strip()
                    else:
                        label = field["name"]

                    field_labels[field["name"]] = label

        except Exception as e:
            print(f"  ⚠️  pdfplumber label extraction failed: {e}")
            return {f["name"]: f["name"] for f in fields_info}

        return field_labels

    # ------------------------------------------------------------------
    # Inspect fillable PDF (geometry + inferred labels — no AI / no OpenAI calls)
    # ------------------------------------------------------------------

    def inspect_fillable_form(self, fillable_pdf_path: str) -> dict:
        """
        List every AcroForm field with inferred human-facing label text.
        Used by products to preview mappings before filling and for Layer 3
        transparency tooling.
        """
        fields_info = self._get_fields_with_coords(fillable_pdf_path)
        if not fields_info:
            return {"fields_detected": 0, "fields": []}

        labels = self._extract_labels_for_fields(fillable_pdf_path, fields_info)
        rows = []
        for f in fields_info:
            ft = f["type"]
            if "/Tx" in ft:
                field_kind = "text"
            elif "/Btn" in ft:
                field_kind = "checkbox"
            else:
                field_kind = "other"
            rows.append(
                {
                    "name": f["name"],
                    "field_type": field_kind,
                    "page": f["page"],
                    "label": labels.get(f["name"], f["name"]),
                    "x0": int(f["x0"]),
                    "x1": int(f["x1"]),
                    "y": int(f["y"]),
                }
            )
        return {"fields_detected": len(rows), "fields": rows}

    # ------------------------------------------------------------------
    # Step 3: Ask AI to map user_data → fields using the clean labels
    # ------------------------------------------------------------------

    def _map_fields_with_ai(
        self,
        fields_info: list[dict],
        field_labels: dict[str, str],
        user_data: dict,
    ) -> Tuple[Dict[str, str], Dict[str, float], bool]:
        """
        Map user_data to PDF field values via the AI, with confidence scores.

        Returns:
            values      - {field_name: value_to_fill}
            confidence  - {field_name: 0.0-1.0}
            cache_hit   - True if result came from template cache

        Cache logic:
          Fingerprint = SHA-256 of sorted {field_name: label} dict.
          On hit: skip LLM, return immediately.
          On miss: call LLM, save result to cache.

        AI prompt asks for:
          {"field_name": {"value": "...", "confidence": 0.0-1.0}}
        Plain string values are also accepted (confidence defaults to 1.0).
        """
        # ── Cache lookup ──────────────────────────────────────────────────────
        fp = self._cache.fingerprint(field_labels)
        cached = self._cache.get(fp)
        if cached is not None:
            values = {k: v.value for k, v in cached.items() if v.value}
            confidence = {k: v.confidence for k, v in cached.items()}
            print(f"  \u2705  Template cache HIT (fp={fp[:8]}\u2026) \u2014 skipping AI call")
            return values, confidence, True

        # ── Detect canonical bundle vs plain flat dict ────────────────────────
        if "flat" in user_data and "structured" in user_data:
            flat_data = user_data["flat"]
            structured_data = user_data["structured"]
        else:
            flat_data = user_data
            structured_data = None

        labeled_fields = []
        for f in fields_info:
            ftype = "textbox" if "/Tx" in f["type"] else "checkbox"
            label = field_labels.get(f["name"], f["name"])
            labeled_fields.append({"field_name": f["name"], "type": ftype, "label": label})

        prompt = (
            "You are a form-filling assistant.\n\n"
            "Each form field below has a label — the text printed next to it.\n\n"
            "Your job:\n"
            "  - Match each user data value to the field whose label best describes it.\n"
            "  - For checkbox fields use exactly 'Yes' or 'No'.\n"
            "  - When similar keys like 'physician_phone' and 'patient_phone' both exist,\n"
            "    use the STRUCTURED DATA section to disambiguate by label context.\n"
            "  - Return ONLY a valid JSON object where every value is an object:\n"
            "      {\"<field_name>\": {\"value\": \"<text_to_fill>\", \"confidence\": <0.0-1.0>}}\n"
            "    confidence 1.0 = certain match, 0.5 = plausible, 0.0 = guessing.\n"
            "  - Omit fields you have no matching data for.\n"
            "  - No markdown, no code fences, no explanation.\n\n"
            f"FORM FIELDS WITH LABELS:\n{json.dumps(labeled_fields, indent=2)}\n\n"
            f"USER DATA (flat, with synonyms):\n{json.dumps(flat_data, indent=2)}\n"
        )
        if structured_data:
            prompt += (
                f"\nUSER DATA (structured, for disambiguation):\n"
                f"{json.dumps(structured_data, indent=2)}\n"
            )

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Return strict JSON only. No markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )

        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            print(f"  \u26a0\ufe0f  AI returned non-JSON: {raw[:300]}")
            return {}, {}, False

        values: Dict[str, str] = {}
        confidence: Dict[str, float] = {}
        cache_entries: Dict[str, MappingEntry] = {}

        for field_name, v in parsed.items():
            if not isinstance(field_name, str):
                continue
            if isinstance(v, dict):
                val = str(v.get("value", ""))
                conf = float(v.get("confidence", 1.0))
            else:
                val = str(v)
                conf = 1.0
            if val:
                values[field_name] = val
                confidence[field_name] = conf
                cache_entries[field_name] = MappingEntry(value=val, confidence=conf)

        if cache_entries:
            self._cache.set(fp, cache_entries, field_labels=field_labels)
            print(f"  \U0001f4be  Template cache STORED (fp={fp[:8]}\u2026, {len(cache_entries)} fields)")

        return values, confidence, False

    # ------------------------------------------------------------------
    # Step 4: Write values into the PDF using direct /V injection
    # ------------------------------------------------------------------

    def _fill_pdf(
        self, input_path: str, output_path: str, field_values: Dict[str, str]
    ) -> bool:
        """
        Inject values into PDF form fields at annotation level.
        Bypasses update_page_form_field_values which crashes on
        commonforms-generated fields (missing font resources).
        Removes /AP so PDF viewers regenerate the visual appearance.
        """
        try:
            from pypdf.generic import TextStringObject, NameObject

            reader = PdfReader(input_path)
            writer = PdfWriter()
            writer.append(reader)

            filled = 0
            for page in writer.pages:
                if "/Annots" not in page:
                    continue
                for annot_ref in page["/Annots"]:
                    try:
                        annot = annot_ref.get_object()
                        field_name_obj = annot.get("/T")
                        if field_name_obj is None:
                            continue
                        field_name = str(field_name_obj)
                        if field_name in field_values and field_values[field_name]:
                            annot[NameObject("/V")] = TextStringObject(
                                field_values[field_name]
                            )
                            if "/AP" in annot:
                                del annot["/AP"]
                            filled += 1
                    except Exception:
                        continue

            print(f"  ✏️  Fields written: {filled}/{len(field_values)}")

            with open(output_path, "wb") as fh:
                writer.write(fh)
            return True
        except Exception as e:
            print(f"  ❌ Error writing filled PDF: {e}")
            return False

    # ------------------------------------------------------------------
    # Public pipeline
    # ------------------------------------------------------------------

    def autofill_pipeline(
        self,
        fillable_pdf_path: str,
        output_path: str,
        user_data: dict,
        dpi: int = 200,
    ) -> dict:
        """
        Full label-aware autofill pipeline:
          1. Extract field bounding boxes (from AcroForm annotations)
          2. Label each field using pdfplumber word proximity
          3. Ask AI to map user_data → field names via semantic label matching
          4. Write filled PDF
        """
        # Step 1
        fields_info = self._get_fields_with_coords(fillable_pdf_path)
        if not fields_info:
            return {
                "success": False,
                "output_path": output_path,
                "error": "No fillable form fields found in PDF",
                "fields_detected": 0,
                "fields_filled": 0,
                "mappings": {},
            }

        # Step 2 — label each field by its nearest left-side words
        field_labels = self._extract_labels_for_fields(fillable_pdf_path, fields_info)
        print("  🏷️  Field labels detected:")
        for f in fields_info:
            ftype = "cb" if "/Btn" in f["type"] else "tx"
            print(f"       [{ftype}] {f['name']:<25} → {field_labels.get(f['name'], '?')}")

        # Step 3 — AI semantic matching (with cache + confidence)
        field_values, confidence, cache_hit = self._map_fields_with_ai(
            fields_info, field_labels, user_data
        )

        # Apply confidence threshold — drop fields the AI is not sure about
        threshold = settings.FILL_CONFIDENCE_THRESHOLD
        if threshold > 0.0:
            skipped = {k: v for k, v in field_values.items()
                       if confidence.get(k, 1.0) < threshold}
            if skipped:
                print(f"  ⚠️  Skipping {len(skipped)} low-confidence fields "
                      f"(threshold={threshold}): {list(skipped.keys())}")
            field_values = {k: v for k, v in field_values.items()
                            if confidence.get(k, 1.0) >= threshold}

        # Step 4 — write PDF
        success = self._fill_pdf(fillable_pdf_path, output_path, field_values)

        # Summary stats
        avg_confidence: Optional[float] = None
        if confidence:
            avg_confidence = round(sum(confidence.values()) / len(confidence), 3)

        fields_skipped = len(confidence) - len(field_values) if threshold > 0.0 else 0

        return {
            "success": success,
            "output_path": output_path,
            "fields_detected": len(fields_info),
            "fields_filled": len(field_values),
            "fields_skipped_low_confidence": fields_skipped,
            "confidence_threshold_used": threshold,
            "mappings": field_values,
            "field_labels": field_labels,
            "confidence": confidence,
            "avg_confidence": avg_confidence,
            "cache_hit": cache_hit,
            "error": None if success else "Failed to write filled PDF",
        }
