"""
Certificate of Electronic Signature — PDF generator.

Produces a human-readable, tamper-evident audit certificate using ReportLab.
This is NOT a PAdES/XAdES cryptographic certificate; it is a workflow-level
evidence document suitable for internal records and simple legal disclosure
under ESIGN / UETA (consult a lawyer for jurisdictional requirements).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    HRFlowable,
    Table,
    TableStyle,
)


_BRAND_BLUE = colors.HexColor("#1e3a8a")
_BRAND_LIGHT = colors.HexColor("#eff6ff")
_ACCENT = colors.HexColor("#3b82f6")
_GRAY = colors.HexColor("#6b7280")
_DARK = colors.HexColor("#111827")
_GREEN = colors.HexColor("#16a34a")

_LEGAL_DISCLOSURE = (
    "This Certificate of Electronic Signature documents a visual signature overlay "
    "applied to the identified PDF document. The signer expressly consented to the use "
    "of an electronic signature and acknowledged that their electronic signature is the "
    "legal equivalent of a handwritten signature in accordance with the Electronic "
    "Signatures in Global and National Commerce Act (ESIGN, 15 U.S.C. § 7001 et seq.) "
    "and the Uniform Electronic Transactions Act (UETA). The SHA-256 hash recorded "
    "herein uniquely identifies the document state at the time of signing. Any "
    "subsequent modification to the document will produce a different hash value, "
    "providing evidence of tampering. This certificate is issued by FillMyPDF and "
    "should be retained alongside the signed document for audit purposes."
)


def generate_certificate(
    *,
    audit_id: str,
    document_filename: str,
    document_hash: str,
    signer_name: Optional[str],
    signer_email: Optional[str],
    signed_at: str,
    client_ip: Optional[str],
    page_index: int,
    signature_mode: str,
    placement: Optional[Dict[str, float]] = None,
    api_key_id: Optional[str] = None,
) -> bytes:
    """Return PDF bytes for the Certificate of Electronic Signature."""

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "CertTitle",
        parent=styles["Heading1"],
        fontSize=22,
        textColor=_BRAND_BLUE,
        spaceAfter=4,
        fontName="Helvetica-Bold",
    )
    subtitle_style = ParagraphStyle(
        "CertSubtitle",
        parent=styles["Normal"],
        fontSize=11,
        textColor=_GRAY,
        spaceAfter=2,
        fontName="Helvetica",
    )
    section_style = ParagraphStyle(
        "SectionHead",
        parent=styles["Normal"],
        fontSize=10,
        textColor=_BRAND_BLUE,
        spaceBefore=14,
        spaceAfter=6,
        fontName="Helvetica-Bold",
        borderPad=4,
    )
    body_style = ParagraphStyle(
        "CertBody",
        parent=styles["Normal"],
        fontSize=9,
        textColor=_DARK,
        fontName="Helvetica",
        leading=14,
    )
    mono_style = ParagraphStyle(
        "CertMono",
        parent=styles["Normal"],
        fontSize=8,
        textColor=_DARK,
        fontName="Courier",
        leading=12,
        wordWrap="CJK",
    )
    legal_style = ParagraphStyle(
        "CertLegal",
        parent=styles["Normal"],
        fontSize=7.5,
        textColor=_GRAY,
        fontName="Helvetica",
        leading=11,
        spaceBefore=8,
    )
    valid_style = ParagraphStyle(
        "Valid",
        parent=styles["Normal"],
        fontSize=10,
        textColor=_GREEN,
        fontName="Helvetica-Bold",
        spaceAfter=4,
    )

    def kv_table(rows: list[tuple[str, str]]) -> Table:
        data = [[Paragraph(f"<b>{k}</b>", body_style), Paragraph(str(v), body_style)] for k, v in rows]
        t = Table(data, colWidths=[2.0 * inch, 5.0 * inch])
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), _BRAND_LIGHT),
                    ("TEXTCOLOR", (0, 0), (0, -1), _BRAND_BLUE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, _BRAND_LIGHT]),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        return t

    # ------------------------------------------------------------------ build
    story: list[Any] = []

    # Header
    story.append(Paragraph("Certificate of Electronic Signature", title_style))
    story.append(Paragraph("Issued by FillMyPDF &nbsp;·&nbsp; Workflow-level evidence document", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=2, color=_BRAND_BLUE, spaceAfter=10))

    # Validity badge
    story.append(Paragraph("✔  SIGNATURE RECORDED", valid_style))

    # ---- Signer information
    story.append(Paragraph("Signer Information", section_style))
    story.append(
        kv_table(
            [
                ("Full Name", signer_name or "(not provided)"),
                ("Email Address", signer_email or "(not provided)"),
                ("Signature Mode", signature_mode.replace("_", " ").title()),
                ("IP Address", client_ip or "(not captured)"),
            ]
        )
    )

    # ---- Document information
    story.append(Paragraph("Document Information", section_style))
    placement_str = (
        f"Page {page_index + 1}  ·  "
        f"X={placement.get('x_pct', 0):.1f}%  "
        f"Y={placement.get('y_pct', 0):.1f}%  "
        f"W={placement.get('width_pct', 0):.1f}%  "
        f"H={placement.get('height_pct', 0):.1f}%"
        if placement
        else f"Page {page_index + 1}"
    )
    story.append(
        kv_table(
            [
                ("Filename", document_filename),
                ("Signed On", signed_at),
                ("Signature Position", placement_str),
                ("API Key ID", api_key_id or "(not recorded)"),
            ]
        )
    )

    # ---- Verification / integrity
    story.append(Paragraph("Integrity Verification", section_style))
    story.append(
        kv_table(
            [
                ("Audit ID", audit_id),
            ]
        )
    )
    story.append(Spacer(1, 6))
    story.append(Paragraph("<b>SHA-256 Document Hash</b>", body_style))
    story.append(Spacer(1, 3))
    story.append(Paragraph(document_hash, mono_style))
    story.append(Spacer(1, 4))
    story.append(
        Paragraph(
            "Recompute <code>sha256(signed_document_bytes)</code> and compare to the hash above "
            "to verify the document has not been modified since signing.",
            body_style,
        )
    )

    # ---- Legal disclosure
    story.append(HRFlowable(width="100%", thickness=0.5, color=_GRAY, spaceBefore=16, spaceAfter=8))
    story.append(Paragraph("Legal Disclosure", section_style))
    story.append(Paragraph(_LEGAL_DISCLOSURE, legal_style))

    # ---- Footer
    story.append(Spacer(1, 12))
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    story.append(
        Paragraph(
            f"Certificate generated: {now_str} &nbsp;·&nbsp; FillMyPDF &nbsp;·&nbsp; "
            f"<i>Retain alongside signed document.</i>",
            legal_style,
        )
    )

    doc.build(story)
    buf.seek(0)
    return buf.getvalue()
