"""
Memory-bounded flat-PDF -> fillable conversion (commonforms / FFDNet).

The stock ``commonforms.prepare_form`` renders every page and runs YOLO
inference on all page-images in ONE batch. On multi-page documents that spikes
memory well past 8 GB. This module instead runs inference **one page at a time**
and releases each page before moving on, so peak memory is bounded by a single
page regardless of document length.

The detector (model) is loaded once per process and reused across requests.
"""
from __future__ import annotations

import gc
import os
from pathlib import Path
from threading import Lock

# commonforms internals (lower-level than prepare_form)
from commonforms.inference import (
    FFDNetDetector,
    render_pdf,
    PyPdfFormCreator,
    sort_widgets,
)

_MODEL = os.getenv("COMMONFORMS_MODEL", "FFDNet-S")
_FAST = os.getenv("COMMONFORMS_FAST", "true").lower() in ("1", "true", "yes")
_IMAGE_SIZE = int(os.getenv("COMMONFORMS_IMAGE_SIZE", "1024"))
_CONFIDENCE = float(os.getenv("COMMONFORMS_CONFIDENCE", "0.1"))
_DEVICE = os.getenv("COMMONFORMS_DEVICE", "cpu")
_MAX_PAGES = int(os.getenv("CONVERT_MAX_PAGES", "40"))

_detector: FFDNetDetector | None = None
_detector_lock = Lock()


def _get_detector() -> FFDNetDetector:
    """Lazily load and cache the FFDNet detector for this process."""
    global _detector
    if _detector is None:
        with _detector_lock:
            if _detector is None:
                _detector = FFDNetDetector(_MODEL, device=_DEVICE, fast=_FAST)
    return _detector


def convert(input_path: str | Path, output_path: str | Path,
            *, use_signature_fields: bool = True, multiline: bool = False) -> int:
    """Convert a flat PDF to a fillable AcroForm, page by page.

    Returns the number of fields injected. Raises on unrecoverable errors.
    """
    detector = _get_detector()
    pages = render_pdf(str(input_path))
    if _MAX_PAGES and len(pages) > _MAX_PAGES:
        pages = pages[:_MAX_PAGES]

    writer = PyPdfFormCreator(str(input_path))
    writer.clear_existing_fields()

    field_count = 0
    # Serialize inference so concurrent requests can't blow up memory together.
    with _detector_lock:
        for page_ix, page in enumerate(pages):
            result = detector.extract_widgets(
                [page], confidence=_CONFIDENCE, image_size=_IMAGE_SIZE
            )
            widgets = result.get(0, [])
            widgets = sort_widgets(widgets)
            for j, w in enumerate(widgets):
                name = f"{w.widget_type.lower()}_{page_ix}_{j}"
                if w.widget_type == "TextBox":
                    writer.add_text_box(name, page_ix, w.bounding_box, multiline=multiline)
                elif w.widget_type == "ChoiceButton":
                    writer.add_checkbox(name, page_ix, w.bounding_box)
                elif w.widget_type == "Signature":
                    if use_signature_fields:
                        writer.add_signature(name, page_ix, w.bounding_box)
                    else:
                        writer.add_text_box(name, page_ix, w.bounding_box)
                field_count += 1
            # Release the page + inference tensors before the next page.
            del result, widgets
            gc.collect()

    writer.save(str(output_path))
    writer.close()
    del pages
    gc.collect()
    return field_count
