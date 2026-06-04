"""Generate FillMyPDF Architecture PDF"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus.flowables import Flowable
from datetime import date

OUTPUT = "/home/user/fillmypdf_v2/FillMyPDF_Architecture.pdf"

# ── Colour palette ──────────────────────────────────────────────────────────
NAVY   = colors.HexColor("#1a2b4a")
BLUE   = colors.HexColor("#2563eb")
LBLUE  = colors.HexColor("#dbeafe")
TEAL   = colors.HexColor("#0d9488")
LTEAL  = colors.HexColor("#ccfbf1")
GRAY   = colors.HexColor("#f1f5f9")
DGRAY  = colors.HexColor("#475569")
GREEN  = colors.HexColor("#16a34a")
LGREEN = colors.HexColor("#dcfce7")
AMBER  = colors.HexColor("#d97706")
LAMBER = colors.HexColor("#fef3c7")
RED    = colors.HexColor("#dc2626")
WHITE  = colors.white
BLACK  = colors.HexColor("#0f172a")

# ── Styles ───────────────────────────────────────────────────────────────────
base = getSampleStyleSheet()

def style(name, **kw):
    s = ParagraphStyle(name, **kw)
    return s

H1 = style("H1", fontSize=22, textColor=WHITE,     fontName="Helvetica-Bold",
           leading=28, alignment=TA_CENTER)
H2 = style("H2", fontSize=14, textColor=NAVY,       fontName="Helvetica-Bold",
           leading=20, spaceBefore=14, spaceAfter=4)
H3 = style("H3", fontSize=11, textColor=BLUE,       fontName="Helvetica-Bold",
           leading=16, spaceBefore=8, spaceAfter=2)
BODY = style("BODY", fontSize=9, textColor=BLACK,   fontName="Helvetica",
             leading=14, spaceAfter=3)
MONO = style("MONO", fontSize=8, textColor=NAVY,    fontName="Courier",
             leading=12, spaceAfter=2, leftIndent=8)
SMALL = style("SMALL", fontSize=8, textColor=DGRAY, fontName="Helvetica",
              leading=12)
LABEL = style("LABEL", fontSize=9, textColor=WHITE, fontName="Helvetica-Bold",
              leading=12, alignment=TA_CENTER)
SUB   = style("SUB", fontSize=8, textColor=DGRAY,   fontName="Helvetica",
              leading=12, alignment=TA_CENTER)


# ── Helpers ──────────────────────────────────────────────────────────────────
def hr(color=BLUE, thickness=1):
    return HRFlowable(width="100%", thickness=thickness, color=color, spaceAfter=6)

def sp(h=6):
    return Spacer(1, h)

def code_block(lines, bg=GRAY):
    rows = [[Paragraph(ln, MONO)] for ln in lines]
    t = Table(rows, colWidths=["100%"])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), bg),
        ("LEFTPADDING",  (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING",   (0,0), (-1,-1), 6),
        ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ("ROUNDEDCORNERS", [4]),
    ]))
    return t

def section_header(text, color=NAVY):
    t = Table([[Paragraph(text, H2)]], colWidths=["100%"])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), LBLUE),
        ("LEFTPADDING",  (0,0), (-1,-1), 12),
        ("TOPPADDING",   (0,0), (-1,-1), 6),
        ("BOTTOMPADDING",(0,0), (-1,-1), 6),
        ("LINEBELOW", (0,0), (-1,-1), 2, BLUE),
    ]))
    return t

def badge(text, bg=TEAL, fg=WHITE):
    t = Table([[Paragraph(text, LABEL)]], colWidths=[3*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,-1), bg),
        ("TOPPADDING",   (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0), (-1,-1), 3),
        ("ROUNDEDCORNERS", [4]),
    ]))
    return t

def info_box(text, bg=LTEAL, border=TEAL):
    t = Table([[Paragraph(text, BODY)]], colWidths=["100%"])
    t.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,-1), bg),
        ("LINEBEFORE",  (0,0), (0,-1), 4, border),
        ("LEFTPADDING", (0,0), (-1,-1), 14),
        ("RIGHTPADDING",(0,0), (-1,-1), 10),
        ("TOPPADDING",  (0,0), (-1,-1), 8),
        ("BOTTOMPADDING",(0,0),(-1,-1), 8),
    ]))
    return t

def two_col(left, right, lw=9*cm, rw=9*cm):
    t = Table([[left, right]], colWidths=[lw, rw])
    t.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING",  (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING",   (0,0), (-1,-1), 0),
        ("BOTTOMPADDING",(0,0), (-1,-1), 0),
        ("INNERGRID", (0,0), (-1,-1), 0, WHITE),
    ]))
    return t


# ── Cover page ───────────────────────────────────────────────────────────────
def cover_page():
    elems = []
    # Title banner
    banner = Table(
        [[Paragraph("FillMyPDF v2", H1)],
         [Paragraph("High-Level Architecture &amp; Feature Reference", style("sub", fontSize=13,
           textColor=colors.HexColor("#bfdbfe"), fontName="Helvetica", leading=18, alignment=TA_CENTER))],
         [Paragraph(f"Generated: {date.today().strftime('%B %d, %Y')}  •  v4.0.0", SUB)]],
        colWidths=["100%"]
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,-1), NAVY),
        ("TOPPADDING",   (0,0), (-1,-1), 28),
        ("BOTTOMPADDING",(0,0), (-1,-1), 28),
        ("LEFTPADDING",  (0,0), (-1,-1), 20),
        ("RIGHTPADDING", (0,0), (-1,-1), 20),
    ]))
    elems += [banner, sp(16)]

    # What is it?
    elems += [
        info_box(
            "<b>FillMyPDF</b> is a production-ready, AI-powered PDF auto-fill SaaS platform. "
            "It lets users store encrypted data profiles and automatically fill PDF forms in "
            "bulk using AI vision mapping — with support for sync/async jobs, template libraries, "
            "e-signatures, webhooks, Chrome extension, and Zapier integration."
        ),
        sp(12),
    ]

    # Pill badges — use cases
    pills = Table([[
        badge("HR Onboarding",   TEAL),
        badge("Tax Forms",       BLUE),
        badge("Real Estate",     colors.HexColor("#7c3aed")),
        badge("Pharma PA Forms", colors.HexColor("#0891b2")),
    ]], colWidths=[4.5*cm, 4.5*cm, 4.5*cm, 4.5*cm])
    pills.setStyle(TableStyle([
        ("ALIGN",  (0,0), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING",  (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
    ]))
    elems += [pills, sp(20)]

    # Quick stats
    stats = Table([
        [
            _stat("50+", "API Endpoints"),
            _stat("30+", "Built-in Templates"),
            _stat("4",   "Worker Threads"),
            _stat("3",   "Cache Layers"),
        ]
    ], colWidths=[4.5*cm]*4)
    stats.setStyle(TableStyle([
        ("ALIGN",  (0,0), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING",  (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
        ("INNERGRID", (0,0), (-1,-1), 1, colors.HexColor("#e2e8f0")),
        ("BOX",       (0,0), (-1,-1), 1, colors.HexColor("#e2e8f0")),
        ("TOPPADDING",   (0,0), (-1,-1), 10),
        ("BOTTOMPADDING",(0,0), (-1,-1), 10),
    ]))
    elems += [stats]
    return elems

def _stat(num, label):
    return [
        Paragraph(f'<font size="18" color="#2563eb"><b>{num}</b></font>', style(
            "sn", fontSize=18, textColor=BLUE, fontName="Helvetica-Bold",
            alignment=TA_CENTER, leading=22)),
        Paragraph(label, style("sl", fontSize=8, textColor=DGRAY,
            fontName="Helvetica", alignment=TA_CENTER, leading=12)),
    ]


# ── Tech Stack ───────────────────────────────────────────────────────────────
def tech_stack():
    elems = [section_header("Tech Stack"), sp(6)]
    rows = [
        [Paragraph("<b>Layer</b>", BODY), Paragraph("<b>Technology</b>", BODY)],
        ["Backend Framework",    "FastAPI 0.104+ / Uvicorn / Python 3.12"],
        ["Data Validation",      "Pydantic v2.5+"],
        ["PDF Processing",       "pypdf, pdf2image, Pillow, reportlab, commonforms"],
        ["AI / Vision",          "Gemini 2.5 Flash (OpenAI-compat endpoint)"],
        ["Security",             "AES-256-GCM encryption, bcrypt, slowapi"],
        ["Storage",              "File-based JSON (no relational database)"],
        ["Async Jobs",           "ThreadPoolExecutor (4 workers)"],
        ["Integrations",         "Chrome Extension (MV3), Zapier Platform CLI"],
        ["Deployment",           "Docker (Python 3.12-slim) + Fly.io (3 GB volume)"],
        ["Testing",              "pytest + pytest-asyncio + pytest-cov (28 files)"],
    ]
    col = [4.5*cm, 13*cm]
    t = Table([[Paragraph(r[0], BODY), Paragraph(r[1], BODY)] if i > 0
               else r for i, r in enumerate(rows)], colWidths=col)
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1, 0), NAVY),
        ("TEXTCOLOR",    (0,0), (-1, 0), WHITE),
        ("FONTNAME",     (0,0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",     (0,0), (-1,-1), 9),
        ("ROWBACKGROUNDS",(0,1),(-1,-1), [WHITE, GRAY]),
        ("GRID",         (0,0), (-1,-1), 0.5, colors.HexColor("#cbd5e1")),
        ("TOPPADDING",   (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0), (-1,-1), 5),
        ("LEFTPADDING",  (0,0), (-1,-1), 8),
    ]))
    elems += [t]
    return elems


# ── System Architecture ───────────────────────────────────────────────────────
def system_architecture():
    elems = [PageBreak(), section_header("System Architecture"), sp(8)]

    layers = [
        (NAVY,  WHITE, "CLIENT LAYER",
         "REST API Clients  •  Chrome Extension (MV3)  •  Zapier Integration"),
        (BLUE,  WHITE, "API GATEWAY LAYER",
         "RequestID Middleware → CORS → Rate Limiter (slowapi, per-tier)\n"
         "Auth: X-API-Key → bcrypt validation → tier lookup"),
        (colors.HexColor("#1d4ed8"), WHITE, "ROUTE LAYER  (FastAPI)",
         "/profiles  /batch  /templates  /jobs  /extract\n"
         "/signing   /keys   /pdf        /billing  /health"),
        (TEAL,  WHITE, "SERVICE LAYER",
         "ProfileService  BatchFillService  TemplateService  APIKeyService\n"
         "JobRunner  VisionService  PDFService  ExtractionService\n"
         "SigningSessionService  EmailService  BillingService"),
        (colors.HexColor("#065f46"), WHITE, "REPOSITORY LAYER",
         "ProfileRepository  APIKeyRepository  TemplateRepository  JobRepository"),
        (colors.HexColor("#1e3a5f"), WHITE, "PERSISTENT STORAGE  (File-based JSON)",
         "profiles/ (AES-256-GCM)  •  api_keys/ (bcrypt)  •  jobs/  •  templates/  •  usage_stats.json"),
    ]

    for bg, fg, title, desc in layers:
        title_style = style(f"lt_{title}", fontSize=9, textColor=fg,
                            fontName="Helvetica-Bold", leading=13, alignment=TA_CENTER)
        desc_style  = style(f"ld_{title}", fontSize=8, textColor=fg,
                            fontName="Helvetica", leading=12, alignment=TA_CENTER)
        t = Table([
            [Paragraph(title, title_style)],
            [Paragraph(desc.replace("\n","<br/>"), desc_style)],
        ], colWidths=["100%"])
        t.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,-1), bg),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
            ("LEFTPADDING",  (0,0), (-1,-1), 10),
            ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ]))
        arrow = Table([[Paragraph("▼", style("arr", fontSize=11, textColor=DGRAY,
                                   fontName="Helvetica", alignment=TA_CENTER, leading=14))]],
                      colWidths=["100%"])
        elems += [t, arrow]

    # External services box
    elems += [sp(4)]
    ext = Table([
        [Paragraph("<b>External Services</b>", style("eh", fontSize=9, textColor=NAVY,
                    fontName="Helvetica-Bold", leading=13))],
        [Paragraph(
            "Gemini 2.5 Flash (OpenAI-compat API)  •  "
            "Stripe (billing, optional)  •  SMTP (e-sign emails, optional)  •  "
            "Outbound Webhooks (HMAC-SHA256 signed, 4 retries)",
            BODY)],
    ], colWidths=["100%"])
    ext.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,-1), LAMBER),
        ("LINEBEFORE",  (0,0), (0,-1), 4, AMBER),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
        ("RIGHTPADDING",(0,0), (-1,-1), 10),
        ("TOPPADDING",  (0,0), (-1,-1), 6),
        ("BOTTOMPADDING",(0,0),(-1,-1), 6),
    ]))
    elems += [ext]
    return elems


# ── PDF Fill Pipeline ─────────────────────────────────────────────────────────
def fill_pipeline():
    elems = [PageBreak(), section_header("Core PDF Fill Pipeline"), sp(8)]

    steps = [
        (LBLUE, BLUE, "1  INPUT NORMALIZATION",
         "User data (JSON / CSV / XLSX) is normalized by InputAdapter into the "
         "Canonical Model — Person, Provider, Organization, or Medication domains — "
         "reducing field-mapping ambiguity."),
        (LGREEN, GREEN, "2  PDF FILLABILITY CHECK",
         "PDFService checks whether the uploaded PDF already has AcroForm fields.\n"
         "• YES → use directly\n"
         "• NO  → commonforms ML model (FFDNet-S) converts static PDF to fillable;\n"
         "         result cached as fillable.pdf for all future fills"),
        (LTEAL, TEAL, "3  LABEL DETECTION  (pdfplumber)",
         "VisionService reads field bounding boxes (pypdf) then uses pdfplumber to "
         "find nearby words on the same horizontal band:\n"
         "• Textbox  → look LEFT  (≤220 px, 4 words)\n"
         "• Checkbox → look RIGHT (≤500 px, 6 words)\n"
         "Falls back to raw field name if no text found."),
        (LAMBER, AMBER, "4  THREE-LAYER CACHE CHECK",
         "Layer 1: In-memory per-request cache\n"
         "Layer 2: Disk cache (TemplateCache JSON — indexed by template+field)\n"
         "Layer 3: AI call to Gemini 2.5 Flash  ← only on cache miss\n"
         "Cache hit → deterministic fills, zero AI cost, zero variance"),
        (colors.HexColor("#fce7f3"), RED, "5  AI FIELD MAPPING  (Gemini 2.5 Flash)",
         "Labeled field list + PDF page image → Gemini 2.5 Flash.\n"
         "Returns: { field_name → value, confidence_score }\n"
         "Fields below FILL_CONFIDENCE_THRESHOLD are dropped (not filled).\n"
         "Result written back to disk cache for future requests."),
        (LGREEN, GREEN, "6  WRITE & OUTPUT",
         "PDFService writes values into AcroForm fields (pypdf / reportlab).\n"
         "Output: ZIP archive (batch) or single PDF stream → client."),
    ]

    for bg, border, title, desc in steps:
        t_style = style(f"ps_{title}", fontSize=9, textColor=border,
                        fontName="Helvetica-Bold", leading=13)
        d_style = style(f"pd_{title}", fontSize=8.5, textColor=BLACK,
                        fontName="Helvetica", leading=13)
        t = Table([
            [Paragraph(title, t_style)],
            [Paragraph(desc.replace("\n","<br/>"), d_style)],
        ], colWidths=["100%"])
        t.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,-1), bg),
            ("LINEBEFORE",  (0,0), (0,-1), 4, border),
            ("LEFTPADDING", (0,0), (-1,-1), 12),
            ("RIGHTPADDING",(0,0), (-1,-1), 10),
            ("TOPPADDING",  (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0),(-1,-1), 6),
        ]))
        elems += [t, sp(5)]

    return elems


# ── Async Jobs ────────────────────────────────────────────────────────────────
def async_jobs():
    elems = [section_header("Async Job Architecture"), sp(8)]

    flow = [
        ("POST /api/v1/jobs/batch",       NAVY, WHITE),
        ("Returns 202 Accepted immediately", BLUE, WHITE),
        ("JobRepository: persist job (status: queued)", TEAL, WHITE),
        ("ThreadPoolExecutor: 4 concurrent workers", colors.HexColor("#7c3aed"), WHITE),
        ("Run full batch fill pipeline per record", DGRAY, WHITE),
        ("Update progress: {total, completed, successful, failed}", DGRAY, WHITE),
        ("On complete: POST webhook (HMAC-SHA256, 4 retries exp. backoff)", GREEN, WHITE),
    ]
    for text, bg, fg in flow:
        fs = style(f"f_{text[:10]}", fontSize=8.5, textColor=fg, fontName="Helvetica",
                   leading=13, alignment=TA_CENTER)
        t = Table([[Paragraph(text, fs)]], colWidths=["100%"])
        t.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,-1), bg),
            ("TOPPADDING",   (0,0), (-1,-1), 5),
            ("BOTTOMPADDING",(0,0), (-1,-1), 5),
        ]))
        arr = Table([[Paragraph("▼", style("a2", fontSize=10, textColor=DGRAY,
                       fontName="Helvetica", alignment=TA_CENTER, leading=13))]],
                    colWidths=["100%"])
        elems += [t, arr]

    elems += [sp(4),
        info_box("Client polls <b>GET /api/v1/jobs/{job_id}</b> for progress, or listens "
                 "for the HMAC-signed webhook POST. Manual replay available via "
                 "<b>POST /api/v1/jobs/{job_id}/webhook-redelivery</b>.")]
    return elems


# ── Security ──────────────────────────────────────────────────────────────────
def security():
    elems = [PageBreak(), section_header("Security Architecture"), sp(8)]

    cols = [
        ("API Keys", NAVY, [
            "Format: fmp_live_&lt;160-bit base32&gt;",
            "Stored:  bcrypt hash (cost 12 prod, 4 test)",
            "Shown:   plaintext once at creation only",
            "Tiers:   free / pro / business / admin",
            "Revoke:  DELETE /api/v1/keys/{id}",
        ]),
        ("Profile Data", TEAL, [
            "Algorithm: AES-256-GCM",
            "Key deriv: PBKDF2-SHA256 (100k iters)",
            "Per-field: random salt + nonce",
            "Auto-detect sensitive fields:",
            "  SSN, EIN, DOB, card numbers",
            "Names/emails stored in plaintext",
        ]),
        ("Webhooks & API", colors.HexColor("#7c3aed"), [
            "Webhook sig: HMAC-SHA256",
            "Header: X-FillMyPDF-Signature",
            "Stripe IPN: verified server-side",
            "Rate limits: per-tier via slowapi",
            "CORS: explicit origins (no wildcard)",
            "Request IDs: full trace correlation",
        ]),
    ]

    cells = []
    for title, color, points in cols:
        th = style(f"sh_{title}", fontSize=10, textColor=WHITE,
                   fontName="Helvetica-Bold", leading=14, alignment=TA_CENTER)
        tb = style(f"sb_{title}", fontSize=8, textColor=BLACK,
                   fontName="Courier", leading=13)
        inner = Table(
            [[Paragraph(title, th)]] + [[Paragraph(p, tb)] for p in points],
            colWidths=[5.5*cm]
        )
        inner.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (0, 0), color),
            ("BACKGROUND", (0,1), (-1,-1), GRAY),
            ("LEFTPADDING",  (0,0), (-1,-1), 8),
            ("RIGHTPADDING", (0,0), (-1,-1), 8),
            ("TOPPADDING",   (0,0), (-1,-1), 5),
            ("BOTTOMPADDING",(0,0), (-1,-1), 5),
            ("BOX", (0,0), (-1,-1), 1, colors.HexColor("#cbd5e1")),
        ]))
        cells.append(inner)

    row = Table([cells], colWidths=[5.9*cm, 5.9*cm, 5.9*cm])
    row.setStyle(TableStyle([
        ("LEFTPADDING",  (0,0), (-1,-1), 3),
        ("RIGHTPADDING", (0,0), (-1,-1), 3),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
    ]))
    elems += [row]
    return elems


# ── Rate Limits / Tiers ───────────────────────────────────────────────────────
def rate_limits():
    elems = [sp(12), section_header("Subscription Tiers & Rate Limits"), sp(6)]
    headers = ["Tier", "Profiles", "Requests / min", "Requests / day"]
    rows = [
        ["free",     "1",         "60",    "10,000"],
        ["pro",      "unlimited", "600",   "100,000"],
        ["business", "unlimited", "6,000", "1,000,000"],
        ["admin",    "unlimited", "100,000","unlimited"],
    ]
    tier_colors = {
        "free":     colors.HexColor("#f1f5f9"),
        "pro":      colors.HexColor("#dbeafe"),
        "business": colors.HexColor("#d1fae5"),
        "admin":    colors.HexColor("#fef3c7"),
    }
    header_row = [Paragraph(f"<b>{h}</b>", BODY) for h in headers]
    data = [header_row] + [[Paragraph(c, BODY) for c in r] for r in rows]
    t = Table(data, colWidths=[3*cm, 4*cm, 4.5*cm, 5.5*cm])
    style_cmds = [
        ("BACKGROUND", (0,0), (-1,0), NAVY),
        ("TEXTCOLOR",  (0,0), (-1,0), WHITE),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,-1), 9),
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#cbd5e1")),
        ("TOPPADDING",   (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0), (-1,-1), 5),
        ("LEFTPADDING",  (0,0), (-1,-1), 8),
    ]
    for i, row in enumerate(rows):
        bg = tier_colors[row[0]]
        style_cmds.append(("BACKGROUND", (0, i+1), (-1, i+1), bg))
    t.setStyle(TableStyle(style_cmds))
    elems += [t]
    return elems


# ── Accuracy Features ─────────────────────────────────────────────────────────
def accuracy_features():
    elems = [PageBreak(), section_header("Accuracy Features"), sp(8)]

    features = [
        ("Coordinate-based Label Detection",
         "Uses pdfplumber to find actual on-page label text next to each field before "
         "calling AI. Avoids relying on internal field names like <i>field_0001</i>."),
        ("Direction-Aware Scanning",
         "Textboxes look LEFT (labeled on left side); far-left checkboxes look RIGHT — "
         "mirrors real PDF layout conventions for highest label accuracy."),
        ("AI Confidence Threshold",
         "<b>FILL_CONFIDENCE_THRESHOLD</b> (default 0.0, recommended 0.5) — fields "
         "mapped below the score are skipped rather than filled with wrong data. "
         "Prevents noisy or hallucinated fills."),
        ("Gemini 2.5 Flash (Multimodal)",
         "Handles visual + text context simultaneously. Receives labeled field list "
         "AND a rendered page image for richer spatial understanding."),
        ("Deterministic Template Cache",
         "Once a form is mapped, results are cached to disk. Subsequent fills are "
         "fully deterministic — no AI variance, zero extra cost, faster turnaround."),
        ("Configurable DPI",
         "PDF-to-image rendered at 200 DPI by default. Higher DPI settings produce "
         "sharper images for improved AI vision quality on complex forms."),
        ("Canonical Data Model",
         "Input normalized to Person / Provider / Org / Med domains before mapping — "
         "reduces field-matching ambiguity by providing structured semantic context."),
        ("AcroForm-Native Path",
         "If the PDF already has AcroForm fields, fills them natively. AI is only used "
         "for field→data mapping, not for reading the PDF — no OCR guesswork."),
    ]

    for title, desc in features:
        t = Table([
            [Paragraph(f"✦  {title}", H3)],
            [Paragraph(desc, BODY)],
        ], colWidths=["100%"])
        t.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,-1), GRAY),
            ("LINEBEFORE",  (0,0), (0,-1), 3, BLUE),
            ("LEFTPADDING", (0,0), (-1,-1), 12),
            ("RIGHTPADDING",(0,0), (-1,-1), 10),
            ("TOPPADDING",  (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ]))
        elems += [t, sp(5)]

    # Config knobs
    elems += [sp(4), Paragraph("Accuracy Configuration Knobs (.env)", H3)]
    elems += [code_block([
        "FILL_CONFIDENCE_THRESHOLD=0.5    # skip low-confidence fields (0.0 = allow all)",
        "DEFAULT_DPI=300                  # higher = sharper images for AI vision",
        "COMMONFORMS_CONFIDENCE=0.1       # static-to-fillable detection sensitivity",
        "DEFAULT_AI_MODEL=gemini-2.5-flash  # swap to more capable model if needed",
        "TEMPLATE_CACHE_ENABLED=true      # persist mapping cache to disk",
        "TEMPLATE_CACHE_TTL_DAYS=0        # 0 = cache never expires",
    ])]
    return elems


# ── Feature Matrix ────────────────────────────────────────────────────────────
def feature_matrix():
    elems = [PageBreak(), section_header("Complete Feature Matrix"), sp(6)]

    features = [
        ("Core Processing",       GREEN,  [
            ("Sync batch fill — JSON / CSV / XLSX",       True),
            ("Static PDF → AcroForm conversion (ML)",     True),
            ("AcroForm field extraction to JSON/CSV",     True),
            ("AI field mapping (Gemini 2.5 Flash)",       True),
            ("Confidence threshold filtering",            True),
            ("PDF merge & split utilities",               True),
        ]),
        ("Async & Jobs",           BLUE,   [
            ("Async job queue (202/poll pattern)",         True),
            ("4 concurrent ThreadPool workers",           True),
            ("Job progress tracking",                     True),
            ("Outbound webhooks (HMAC-SHA256)",           True),
            ("Webhook auto-retry (exp. backoff × 4)",     True),
            ("Manual webhook redelivery",                 True),
        ]),
        ("Templates & Caching",    TEAL,   [
            ("Pre-built template library (30+ forms)",    True),
            ("Template upload/manage (admin)",            True),
            ("3-layer mapping cache (memory+disk)",       True),
            ("Lazy fillable PDF conversion + cache",      True),
            ("Template batch fill",                       True),
        ]),
        ("Security & Auth",        NAVY,   [
            ("API key auth (X-API-Key)",                  True),
            ("bcrypt key hashing (cost 12)",              True),
            ("AES-256-GCM profile encryption",            True),
            ("PBKDF2 key derivation (100k iters)",        True),
            ("Per-tier rate limiting (slowapi)",          True),
            ("Request ID correlation",                    True),
        ]),
        ("Integrations",           colors.HexColor("#7c3aed"), [
            ("Chrome Extension (Manifest V3)",            True),
            ("Zapier Platform CLI integration",           True),
            ("Stripe billing (checkout + portal)",        True),
            ("SMTP e-sign notifications",                 True),
            ("OpenAPI docs (Swagger + ReDoc)",            True),
        ]),
        ("E-Signatures",           AMBER,  [
            ("Visual signature overlay (PNG stamp)",      True),
            ("Multi-signature session management",        True),
            ("Signature audit log",                       True),
            ("Certificate-based PAdES signing",          False),
        ]),
    ]

    for category, color, items in features:
        cat_style = style(f"cat_{category}", fontSize=9, textColor=WHITE,
                          fontName="Helvetica-Bold", leading=13)
        item_style = style(f"it_{category}", fontSize=8.5, textColor=BLACK,
                           fontName="Helvetica", leading=13)
        tick_style = style(f"tk_{category}", fontSize=9,
                           fontName="Helvetica-Bold", leading=13, alignment=TA_CENTER)

        inner_rows = [
            [Paragraph(f"  {name}", item_style),
             Paragraph("<font color='#16a34a'>✓</font>" if ok
                       else "<font color='#94a3b8'>—</font>", tick_style)]
            for name, ok in items
        ]

        header = Table([[Paragraph(category, cat_style)]], colWidths=[17*cm])
        header.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,-1), color),
            ("LEFTPADDING",  (0,0), (-1,-1), 10),
            ("TOPPADDING",   (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0), (-1,-1), 4),
        ]))

        body = Table(inner_rows, colWidths=[15.5*cm, 1.5*cm])
        body.setStyle(TableStyle([
            ("ROWBACKGROUNDS", (0,0), (-1,-1), [WHITE, GRAY]),
            ("GRID",  (0,0), (-1,-1), 0.3, colors.HexColor("#e2e8f0")),
            ("LEFTPADDING",  (0,0), (-1,-1), 8),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING",   (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0), (-1,-1), 4),
        ]))

        elems += [header, body, sp(8)]

    return elems


# ── API Quick Reference ───────────────────────────────────────────────────────
def api_reference():
    elems = [PageBreak(), section_header("API Quick Reference"), sp(6)]

    groups = [
        ("System (no auth)", DGRAY, [
            ("GET",    "/",                                  "Service info"),
            ("GET",    "/health",                            "Health check"),
            ("GET",    "/usage",                             "Global usage stats"),
        ]),
        ("API Keys (admin only)", NAVY, [
            ("POST",   "/api/v1/keys",                       "Create API key"),
            ("GET",    "/api/v1/keys",                       "List keys"),
            ("DELETE", "/api/v1/keys/{id}",                  "Revoke key"),
        ]),
        ("Profiles", BLUE, [
            ("POST",   "/api/v1/profiles",                   "Create profile"),
            ("GET",    "/api/v1/profiles",                   "List profiles"),
            ("GET",    "/api/v1/profiles/{id}",              "Get profile"),
            ("PATCH",  "/api/v1/profiles/{id}",              "Update profile"),
            ("DELETE", "/api/v1/profiles/{id}",              "Delete profile"),
            ("POST",   "/api/v1/profiles/import",            "Bulk CSV/Excel import"),
        ]),
        ("Batch (sync)", TEAL, [
            ("POST",   "/api/v1/batch/fill-json",            "Fill from JSON array"),
            ("POST",   "/api/v1/batch/fill-csv",             "Fill from CSV"),
            ("POST",   "/api/v1/batch/fill-xlsx",            "Fill from Excel"),
            ("GET",    "/api/v1/batch/download/{file}",      "Download result ZIP"),
        ]),
        ("Async Jobs", colors.HexColor("#7c3aed"), [
            ("POST",   "/api/v1/jobs/batch",                 "Submit async batch (202)"),
            ("GET",    "/api/v1/jobs/{id}",                  "Poll job status"),
            ("POST",   "/api/v1/jobs/{id}/webhook-redelivery","Replay webhook"),
            ("DELETE", "/api/v1/jobs/{id}",                  "Cancel job"),
        ]),
        ("Templates", GREEN, [
            ("GET",    "/api/v1/templates",                  "List templates"),
            ("POST",   "/api/v1/templates/{id}/fill",        "Single-record fill"),
            ("POST",   "/api/v1/templates/{id}/fill-batch",  "Batch fill"),
            ("POST",   "/api/v1/templates/upload",           "Upload template (admin)"),
        ]),
        ("Extract / Sign / PDF", AMBER, [
            ("POST",   "/api/v1/extract/fields",             "AcroForm → JSON"),
            ("POST",   "/api/v1/extract/csv",                "AcroForm → CSV"),
            ("POST",   "/api/v1/signing/overlay",            "Visual signature stamp"),
            ("POST",   "/api/v1/pdf/merge",                  "Merge PDFs"),
            ("POST",   "/api/v1/pdf/split",                  "Split PDF"),
        ]),
    ]

    method_colors = {
        "GET":    colors.HexColor("#16a34a"),
        "POST":   colors.HexColor("#2563eb"),
        "PATCH":  colors.HexColor("#d97706"),
        "DELETE": colors.HexColor("#dc2626"),
    }

    for group, color, endpoints in groups:
        gh = style(f"gh_{group}", fontSize=8.5, textColor=WHITE,
                   fontName="Helvetica-Bold", leading=12)
        header = Table([[Paragraph(group, gh)]], colWidths=[17*cm])
        header.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,-1), color),
            ("LEFTPADDING",  (0,0), (-1,-1), 10),
            ("TOPPADDING",   (0,0), (-1,-1), 3),
            ("BOTTOMPADDING",(0,0), (-1,-1), 3),
        ]))

        ep_rows = []
        for method, path, desc in endpoints:
            mc = method_colors.get(method, DGRAY)
            ms = style(f"ms_{method}_{path}", fontSize=8, textColor=WHITE,
                       fontName="Helvetica-Bold", leading=11, alignment=TA_CENTER)
            ps = style(f"ps_{path}", fontSize=8, textColor=NAVY,
                       fontName="Courier", leading=11)
            ds = style(f"ds_{path}", fontSize=8, textColor=DGRAY,
                       fontName="Helvetica", leading=11)
            mt = Table([[Paragraph(method, ms)]], colWidths=[1.4*cm])
            mt.setStyle(TableStyle([
                ("BACKGROUND",   (0,0), (-1,-1), mc),
                ("TOPPADDING",   (0,0), (-1,-1), 2),
                ("BOTTOMPADDING",(0,0), (-1,-1), 2),
                ("LEFTPADDING",  (0,0), (-1,-1), 3),
                ("RIGHTPADDING", (0,0), (-1,-1), 3),
            ]))
            ep_rows.append([mt, Paragraph(path, ps), Paragraph(desc, ds)])

        body = Table(ep_rows, colWidths=[1.6*cm, 8.5*cm, 6.9*cm])
        body.setStyle(TableStyle([
            ("ROWBACKGROUNDS", (0,0), (-1,-1), [WHITE, GRAY]),
            ("LEFTPADDING",  (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING",   (0,0), (-1,-1), 3),
            ("BOTTOMPADDING",(0,0), (-1,-1), 3),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ]))

        elems += [header, body, sp(6)]

    return elems


# ── Environment Variables ─────────────────────────────────────────────────────
def env_variables():
    elems = [PageBreak(), section_header("Key Environment Variables"), sp(8)]
    elems += [code_block([
        "# Security (REQUIRED in production)",
        "PROFILES_ENCRYPTION_KEY=&lt;32+ char random key&gt;",
        "",
        "# Server",
        "API_HOST=0.0.0.0",
        "API_PORT=8000",
        "DEBUG=False",
        "",
        "# AI / Vision",
        "DEFAULT_AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/",
        "DEFAULT_AI_MODEL=gemini-2.5-flash",
        "DEFAULT_DPI=200",
        "",
        "# Accuracy",
        "FILL_CONFIDENCE_THRESHOLD=0.5   # skip low-confidence AI mappings",
        "",
        "# Async Jobs",
        "JOB_WORKER_THREADS=4",
        "",
        "# Webhooks",
        "WEBHOOK_SIGNING_SECRET=&lt;optional HMAC secret&gt;",
        "WEBHOOK_MAX_ATTEMPTS=4",
        "WEBHOOK_RETRY_BASE_DELAY_SEC=1.0",
        "",
        "# Stripe (optional)",
        "STRIPE_SECRET_KEY=sk_...",
        "STRIPE_PUBLISHABLE_KEY=pk_...",
        "STRIPE_WEBHOOK_SECRET=whsec_...",
        "",
        "# SMTP — e-sign notifications (optional)",
        "SMTP_HOST=smtp.example.com",
        "SMTP_PORT=587",
        "SMTP_USERNAME=user@example.com",
        "SMTP_PASSWORD=secret",
        "APP_BASE_URL=https://fillmypdf.example.com",
    ])]
    return elems


# ── Build PDF ─────────────────────────────────────────────────────────────────
def build():
    doc = SimpleDocTemplate(
        OUTPUT,
        pagesize=A4,
        leftMargin=1.8*cm,
        rightMargin=1.8*cm,
        topMargin=1.8*cm,
        bottomMargin=1.8*cm,
        title="FillMyPDF v2 — Architecture & Feature Reference",
        author="FillMyPDF",
        subject="High-Level Architecture",
    )

    story = []
    story += cover_page()
    story += tech_stack()
    story += system_architecture()
    story += fill_pipeline()
    story += async_jobs()
    story += security()
    story += rate_limits()
    story += accuracy_features()
    story += feature_matrix()
    story += api_reference()
    story += env_variables()

    doc.build(story)
    print(f"PDF generated: {OUTPUT}")

if __name__ == "__main__":
    build()
