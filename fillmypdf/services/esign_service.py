"""
Visual e-signature overlay (MVP)
===============================
Stamps a PNG onto a PDF page inside a rectangle expressed as **percentages** of the
page’s MediaBox width/height (**origin bottom-left**, matching PDF conventions).

Creates a temporary overlay PDF with ReportLab (`mask='auto'` for transparency) then
uses ``Page.merge_page()`` from ``pypdf``. This is **not** certificate-based (PAdES)
signing — only a graphical stamp for workflow/UI needs.
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


class ESignValidationError(ValueError):
    pass


def page_sizes_pts(pdf_path: Path | str) -> Dict[int, Tuple[float, float]]:
    """``{page_index: (width_pts, height_pts)}`` from the PDF MediaBox."""
    reader = PdfReader(str(pdf_path))
    out: Dict[int, Tuple[float, float]] = {}
    for i, page in enumerate(reader.pages):
        mb = page.mediabox
        out[i] = (float(mb.width), float(mb.height))
    return out


def placement_from_acro_field(
    field: dict,
    page_w: float,
    page_h: float,
    *,
    min_height_pct: float = 4.0,
) -> Optional[dict]:
    """Convert a ``_get_fields_with_coords`` widget (top-origin) to e-sign %.

    E-sign percentages use PDF bottom-left origin (same as ``/signatures/apply``).

    Thin AcroForm signature lines (~1–2% tall) are bumped to ``min_height_pct``.
    Growth is **downward** from the widget top so the stamp stays on the blank
    and does not cover the printed label above (TDI Section IV, etc.).
    """
    if page_w <= 0 or page_h <= 0:
        return None
    try:
        x0 = float(field.get("x0") if field.get("x0") is not None else field.get("x") or 0)
        x1 = float(field.get("x1") if field.get("x1") is not None else x0)
        y_top = float(field.get("y") or 0)
        y_bottom = float(field.get("y_bottom") if field.get("y_bottom") is not None else y_top)
    except (TypeError, ValueError):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y_bottom < y_top:
        y_top, y_bottom = y_bottom, y_top

    # Top-origin → PDF bottom-left (pdf_y0 = widget bottom, pdf_y1 = widget top)
    pdf_y0 = page_h - y_bottom
    pdf_y1 = page_h - y_top
    x_pct = round((x0 / page_w) * 100, 2)
    width_pct = round(((x1 - x0) / page_w) * 100, 2)
    height_pct = round(((pdf_y1 - pdf_y0) / page_h) * 100, 2)
    if width_pct < 0.5:
        return None

    if height_pct < min_height_pct:
        height_pct = min_height_pct
        # Keep widget TOP; lower the box bottom (grow down the page).
        top_pct = (pdf_y1 / page_h) * 100.0
        y_pct = round(top_pct - height_pct, 2)
        if y_pct < 0:
            y_pct = 0.0
            height_pct = min(height_pct, round(100.0 - y_pct, 2))
            # Last resort: if the page bottom clips the box, nudge up slightly.
            if y_pct + height_pct < top_pct:
                height_pct = round(min(min_height_pct, top_pct), 2)
                y_pct = round(max(0.0, top_pct - height_pct), 2)
    else:
        y_pct = round((pdf_y0 / page_h) * 100, 2)

    if y_pct + height_pct > 100:
        height_pct = max(0.5, round(100.0 - y_pct, 2))
    if x_pct + width_pct > 100:
        width_pct = max(0.5, round(100.0 - x_pct, 2))
    return {
        "page_index": int(field.get("page") or 0),
        "x_pct": x_pct,
        "y_pct": y_pct,
        "width_pct": width_pct,
        "height_pct": height_pct,
    }


def enrich_signature_placements(
    signatures: List[dict],
    fields_info: List[dict],
    pdf_path: Path | str,
) -> List[dict]:
    """Attach e-sign placement to intake signature dicts from AcroForm geometry."""
    if not signatures:
        return signatures
    sizes = page_sizes_pts(pdf_path)
    by_name = {f.get("name"): f for f in fields_info if f.get("name")}
    out: List[dict] = []
    for s in signatures:
        row = dict(s)
        acro = row.get("acro_field") or row.get("field")
        f = by_name.get(acro)
        if f is not None:
            page = int(f.get("page") or 0)
            pw, ph = sizes.get(page, (0.0, 0.0))
            place = placement_from_acro_field(f, pw, ph)
            if place:
                row["placement"] = place
        out.append(row)
    return out


def _bbox_pts(
    page_w: float,
    page_h: float,
    *,
    x_pct: float,
    y_pct: float,
    width_pct: float,
    height_pct: float,
) -> Tuple[float, float, float, float]:
    if min(x_pct, y_pct, width_pct, height_pct) < 0 or max(x_pct, y_pct, width_pct, height_pct) > 100:
        raise ESignValidationError("Signature box percentages must be in [0, 100].")
    if x_pct + width_pct > 100.01 or y_pct + height_pct > 100.01:
        raise ESignValidationError("Signature box overflows the page.")

    x = page_w * (x_pct / 100.0)
    y = page_h * (y_pct / 100.0)
    w = page_w * (width_pct / 100.0)
    h = page_h * (height_pct / 100.0)
    return x, y, w, h


_CURSIVE_FONT_PATHS = [
    "/usr/share/fonts/truetype/freefont/FreeSerifItalic.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
    "DejaVuSerif-Italic.ttf",
]


def _load_cursive_font(size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    for path in _CURSIVE_FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def typed_name_to_png(text: str, *, font_px: int = 52, max_chars: int = 120) -> bytes:
    t = text.strip().replace("\n", " ").replace("\r", " ")
    if not t:
        raise ESignValidationError("signature_text is empty.")
    if len(t) > max_chars:
        t = t[: max_chars - 3] + "..."

    font = _load_cursive_font(font_px)

    # Measure actual text size to fit tightly
    tmp = Image.new("RGBA", (1, 1), (255, 255, 255, 0))
    tmp_draw = ImageDraw.Draw(tmp)
    bbox = tmp_draw.textbbox((0, 0), t, font=font)
    text_w = bbox[2] - bbox[0] + 2
    text_h = bbox[3] - bbox[1] + 2

    pad_x, pad_y = 12, 10
    wpx = max(text_w + pad_x * 2, 200)
    hpx = text_h + pad_y * 2

    img = Image.new("RGBA", (wpx, hpx), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    # Dark navy blue — looks like ink
    draw.text((pad_x - bbox[0], pad_y - bbox[1]), t, fill=(15, 40, 100, 230), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _build_overlay_pdf(
    page_w: float,
    page_h: float,
    png_bytes: bytes,
    *,
    x: float,
    y: float,
    box_w: float,
    box_h: float,
    timestamp_text: "str | None" = None,
) -> io.BytesIO:
    overlay = io.BytesIO()
    c = canvas.Canvas(overlay, pagesize=(page_w, page_h))
    ir = ImageReader(io.BytesIO(png_bytes))
    thumb = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    tw, th = thumb.size
    if tw <= 0 or th <= 0:
        raise ESignValidationError("Signature image has no pixels.")

    # Reserve space at the bottom of the box for the timestamp line
    TIMESTAMP_FONT_SIZE = max(6.0, box_h * 0.15)
    timestamp_line_h = TIMESTAMP_FONT_SIZE + 2 if timestamp_text else 0.0
    img_box_h = box_h - timestamp_line_h

    scale = min(box_w / float(tw), max(1.0, img_box_h) / float(th))
    draw_w = tw * scale
    draw_h = th * scale
    # Left-align and baseline the ink just above the timestamp — PA signature
    # lines are wide; centering floated short names mid-line and looked wrong.
    inset_x = x + 2.0
    inset_y = y + timestamp_line_h

    c.drawImage(ir, inset_x, inset_y, width=draw_w, height=draw_h, mask="auto")

    if timestamp_text:
        c.setFont("Helvetica", TIMESTAMP_FONT_SIZE)
        c.setFillColorRGB(0.3, 0.3, 0.3)  # dark grey — subtle
        # Left-align under the signature; clip to box width
        c.drawString(x + 2, y + 1, timestamp_text)

    c.showPage()
    c.save()
    overlay.seek(0)
    return overlay


def apply_signature_overlay(
    input_pdf_path: Path,
    output_pdf_path: Path,
    *,
    png_bytes: bytes,
    page_index: int,
    x_pct: float,
    y_pct: float,
    width_pct: float,
    height_pct: float,
    audit_id: str = "",
    signer_name: str = "",
    signer_email: str = "",
    include_timestamp: bool = True,
    timestamp_override: "str | None" = None,
) -> str:
    """Apply overlay, embed metadata, write output, return SHA-256 hex digest.

    When ``include_timestamp`` is True a small date/time line is drawn at the
    bottom of the signature box, e.g. "Signed: 2026-05-30 14:22 UTC".
    Pass ``timestamp_override`` to supply a custom string (useful in tests).
    """
    reader = PdfReader(str(input_pdf_path))
    npages = len(reader.pages)
    if npages == 0:
        raise ESignValidationError("PDF has no pages.")
    if page_index < 0 or page_index >= npages:
        raise ESignValidationError(f"page_index {page_index} out of range (0..{npages - 1}).")

    base_page = reader.pages[page_index]
    mb = base_page.mediabox
    page_w = float(mb.width)
    page_h = float(mb.height)

    # Build the timestamp string that will be rendered on the overlay
    signed_at = datetime.now(timezone.utc)
    signed_at_iso = signed_at.isoformat()
    if include_timestamp:
        ts_text = timestamp_override or signed_at.strftime("Signed: %Y-%m-%d %H:%M UTC")
    else:
        ts_text = None

    x, y, bw, bh = _bbox_pts(
        page_w, page_h, x_pct=x_pct, y_pct=y_pct, width_pct=width_pct, height_pct=height_pct
    )
    overlay_buf = _build_overlay_pdf(
        page_w, page_h, png_bytes, x=x, y=y, box_w=bw, box_h=bh, timestamp_text=ts_text
    )
    overlay_pdf = PdfReader(overlay_buf)
    overlay_pg = overlay_pdf.pages[0]

    writer = PdfWriter()
    writer.append(reader)
    writer.pages[page_index].merge_page(overlay_pg)

    # Embed signing metadata into PDF Info dictionary for tamper-evidence
    writer.add_metadata(
        {
            "/Producer": "FillMyPDF e-Sign Service",
            "/Creator": "FillMyPDF",
            "/FillMyPDF_AuditID": audit_id,
            "/FillMyPDF_SignedAt": signed_at_iso,
            "/FillMyPDF_SignerName": signer_name or "",
            "/FillMyPDF_SignerEmail": signer_email or "",
        }
    )

    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_buf = io.BytesIO()
    writer.write(pdf_buf)
    pdf_bytes = pdf_buf.getvalue()

    with open(output_pdf_path, "wb") as fh:
        fh.write(pdf_bytes)

    return hashlib.sha256(pdf_bytes).hexdigest()
