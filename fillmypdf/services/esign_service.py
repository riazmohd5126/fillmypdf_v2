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
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


class ESignValidationError(ValueError):
    pass


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
) -> io.BytesIO:
    overlay = io.BytesIO()
    c = canvas.Canvas(overlay, pagesize=(page_w, page_h))
    ir = ImageReader(io.BytesIO(png_bytes))
    thumb = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    tw, th = thumb.size
    if tw <= 0 or th <= 0:
        raise ESignValidationError("Signature image has no pixels.")

    scale = min(box_w / float(tw), box_h / float(th))
    draw_w = tw * scale
    draw_h = th * scale
    inset_x = x + max(0.0, (box_w - draw_w) / 2.0)
    inset_y = y + max(0.0, (box_h - draw_h) / 2.0)

    c.drawImage(ir, inset_x, inset_y, width=draw_w, height=draw_h, mask="auto")
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
) -> str:
    """Apply overlay, embed metadata, write output, return SHA-256 hex digest."""
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

    x, y, bw, bh = _bbox_pts(
        page_w, page_h, x_pct=x_pct, y_pct=y_pct, width_pct=width_pct, height_pct=height_pct
    )
    overlay_buf = _build_overlay_pdf(page_w, page_h, png_bytes, x=x, y=y, box_w=bw, box_h=bh)
    overlay_pdf = PdfReader(overlay_buf)
    overlay_pg = overlay_pdf.pages[0]

    writer = PdfWriter()
    writer.append(reader)
    writer.pages[page_index].merge_page(overlay_pg)

    # Embed signing metadata into PDF Info dictionary for tamper-evidence
    signed_at = datetime.now(timezone.utc).isoformat()
    writer.add_metadata(
        {
            "/Producer": "FillMyPDF e-Sign Service",
            "/Creator": "FillMyPDF",
            "/FillMyPDF_AuditID": audit_id,
            "/FillMyPDF_SignedAt": signed_at,
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
