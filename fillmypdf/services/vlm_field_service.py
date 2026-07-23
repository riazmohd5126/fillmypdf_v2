"""
VLM Field Service - local vision-language field understanding
=============================================================
Reads a rendered PDF page image plus the detected field boxes and asks a
LOCAL Qwen2.5-VL model (served by Ollama/vLLM) to produce the human-facing
label, section header, and checkbox group for each field.

This is the "understanding" engine: it generalises across form layouts far
better than the geometry heuristics, without the per-form tuning.

Privacy
-------
This service is LOCAL ONLY.  The caller resolves its (api_key, base_url,
model) via `ai_provider.prepare_local_vision_config()`, which hard-pins the
base_url to a private/loopback host and raises otherwise.  It therefore never
sends page images (which may contain PHI) to Gemini or any external host.

Output contract
---------------
`understand_fields()` returns the same `label_data` mapping the geometry path
produces, so `inspect_fillable_form` consumes it unchanged:

    { field_name: {"label": str, "source": "vlm_local",
                   "section": str|None, "group": str|None} }
"""

from __future__ import annotations

import base64
import json

from openai import OpenAI

from .vision_service import _clean_label


class VLMFieldService:
    """Local Qwen-VL powered field understanding (label / section / group)."""

    def __init__(self, api_key: str, base_url: str, model: str, dpi: int = 200):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.dpi = dpi

    def understand_fields(
        self, pdf_path: str, fields_info: list[dict]
    ) -> dict[str, dict]:
        """
        Return {field_name: {label, source, section, group}} for as many
        fields as the model resolves.  Any page/parse failure is swallowed and
        that page's fields are simply omitted, so the caller can fall back to
        geometry for the rest.
        """
        try:
            import fitz  # PyMuPDF
        except Exception as exc:  # pragma: no cover - env dependent
            print(f"  vlm_local: PyMuPDF unavailable ({exc}); skipping VLM pass")
            return {}

        scale = self.dpi / 72.0

        by_page: dict[int, list[dict]] = {}
        for f in fields_info:
            by_page.setdefault(f["page"], []).append(f)

        resolved: dict[str, dict] = {}
        try:
            client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        except Exception as exc:
            print(f"  vlm_local: could not init client ({exc})")
            return {}

        doc = fitz.open(pdf_path)
        try:
            for pno, page_fields in by_page.items():
                if pno >= doc.page_count:
                    continue
                try:
                    page = doc[pno]
                    pix = page.get_pixmap(dpi=self.dpi)
                    b64 = base64.standard_b64encode(pix.tobytes("png")).decode()
                    data_uri = f"data:image/png;base64,{b64}"

                    fields_text = "\n".join(
                        f"- {f['name']} ({'checkbox' if '/Btn' in f.get('type','') else 'text'}) "
                        f": [{round(f['x0']*scale,1)}, {round(f['y']*scale,1)}, "
                        f"{round(f['x1']*scale,1)}, {round(f.get('y_bottom', f['y'])*scale,1)}]"
                        for f in page_fields
                    )
                    prompt = (
                        "You are reading one page of a fillable PDF form. "
                        "Each field below is given as `name (type) : pixel box "
                        "[x0,y0,x1,y1]` on the attached page image.\n"
                        "For EACH field return:\n"
                        "  label   - the printed caption for that field\n"
                        "  section - the nearest section/heading above it, or null\n"
                        "  group   - for a checkbox, the shared question/column "
                        "header its option belongs to, else null\n"
                        "Respond with STRICT JSON only, no markdown:\n"
                        "{\"<name>\": {\"label\": \"..\", \"section\": \"..\"|null, "
                        "\"group\": \"..\"|null}}\n\n"
                        f"Fields:\n{fields_text}"
                    )

                    resp = client.chat.completions.create(
                        model=self.model,
                        temperature=0.0,
                        max_tokens=2000,
                        messages=[
                            {"role": "system", "content": "Return strict JSON only. No markdown."},
                            {"role": "user", "content": [
                                {"type": "image_url", "image_url": {"url": data_uri}},
                                {"type": "text", "text": prompt},
                            ]},
                        ],
                    )
                    raw = (resp.choices[0].message.content or "").strip()
                    if raw.startswith("```"):
                        raw = raw.split("```")[1].lstrip("json").strip()
                    parsed = json.loads(raw)
                    names_on_page = {f["name"] for f in page_fields}
                    for name, val in parsed.items():
                        if name not in names_on_page or not isinstance(val, dict):
                            continue
                        label = _clean_label(str(val.get("label") or ""))
                        section = val.get("section")
                        group = val.get("group")
                        resolved[name] = {
                            "label": label,
                            "source": "vlm_local",
                            "section": (str(section).strip() or None) if section else None,
                            "group": (str(group).strip() or None) if group else None,
                        }
                except Exception as exc:
                    print(f"  vlm_local: page {pno} failed ({exc}); falling back")
                    continue
        finally:
            doc.close()

        return resolved
