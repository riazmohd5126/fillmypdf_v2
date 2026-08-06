"""
Vision Service - AI-powered PDF auto-fill
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Dict, Optional, Tuple

from openai import OpenAI
from pypdf import PdfReader, PdfWriter

from .template_cache import TemplateCache
from .field_map_cache import FieldMapCache
from .label_cache import LabelCache
from .canonical_field_service import CanonicalFieldService
from ..config import settings


# ---------------------------------------------------------------------------
# Label text cleanup helpers
# ---------------------------------------------------------------------------

# Checkbox / dingbat glyphs that pdfplumber returns as ordinary "words"
# because they are drawn as font glyphs (ZapfDingbats / Wingdings), not as
# real checkbox widgets. These pollute inferred labels (e.g. "☐ M ☐ F").
_GLYPH_CHARS = "☐☑☒□■◻◼❑▢✓✔✗✘✕●○◯◦⬜⬛"


def _strip_glyphs(text: str) -> str:
    """Remove checkbox/dingbat glyph characters from a word/label, including
    Private-Use-Area codepoints (U+F000-U+F8FF) that PDF viewers commonly
    remap Wingdings/Symbol-font dingbats to (e.g. \uf0d8 a pointing-hand
    arrow, \uf0fc a checkmark) — these render as printed "mark this" cues
    next to a field/checkbox and must never survive into an inferred label
    (e.g. "\uf0d8 Member Last Name", "Mark \uf0fc or X: ACE")."""
    for ch in _GLYPH_CHARS:
        text = text.replace(ch, "")
    return "".join(ch for ch in text if not (0xF000 <= ord(ch) <= 0xF8FF))


def _is_glyph_word(text: str) -> bool:
    """True if a word is nothing but glyph characters / whitespace."""
    return _strip_glyphs(text).strip() == ""


def _is_separator_word(text: str) -> bool:
    """True if a word carries NO alphanumeric content — i.e. it is pure
    punctuation/separator glyphs like "/", "-", ":", "( )", "//". These sit
    BETWEEN the boxes of a split composite field (a date "MM / DD / YYYY", an
    SSN, a phone "( ) -") and must never be mistaken for that box's label."""
    stripped = _strip_glyphs(text)
    return not any(ch.isalnum() for ch in stripped)


_SECTION_TITLE_RE = re.compile(
    r"^(SECTION|PART|STEP)\b", re.IGNORECASE
)
_ROMAN_NUMERAL_TOKEN_RE = re.compile(r"^(?:[IVX]+|[ivx]+)$")


_HEADINGISH_SINGLE_WORD_ALLOWLIST = {"REQUEST"}


def _is_headingish(text: str) -> bool:
    """True for a colon-less run that reads like a LOCAL checkbox-group
    heading (short, e.g. "REQUEST", "LINE OF BUSINESS") rather than a
    page-level SECTION/form title that happens to have a checkbox cluster
    somewhere beneath it on the page (e.g. "SECTION II — REASON FOR
    REQUEST", "STEP THERAPY EXCEPTION REQUEST FORM"), a fragment of a longer
    sentence/disclaimer that happens to end its line right above a checkbox
    (e.g. "...may result in denial OR DISMISSAL OF REQUEST."), or a bare
    ALL-CAPS acronym/connective word coincidentally sitting near a checkbox
    (e.g. "FDA", "AND", "CRITERIA") — none of these are real group headers.
    Used to gate acceptance of a colon-less column-grid header, which —
    unlike the ":"-terminated case — has no punctuation cue of its own."""
    if not text or not (3 <= len(text) <= 25):
        return False
    if any(ch.isdigit() for ch in text) or "_" in text or "#" in text:
        return False
    # A trailing full stop is sentence-final punctuation — real headers/
    # titles never end a line with "." (unlike a truncated disclaimer
    # fragment that happens to be the last words of a sentence).
    if text.rstrip().endswith("."):
        return False
    if _SECTION_TITLE_RE.match(text):
        return False
    words = text.split()
    if any(_ROMAN_NUMERAL_TOKEN_RE.match(w) for w in words):
        return False
    # A single bare word is almost always a stray acronym/connective
    # ("FDA", "AND", "CRITERIA") rather than a real governing header —
    # only accept it for the specific known-good case ("REQUEST").
    if len(words) == 1:
        return text.upper() in _HEADINGISH_SINGLE_WORD_ALLOWLIST
    letters_only = text.replace(" ", "")
    if letters_only.isupper():
        return True
    return all(w[:1].isupper() for w in words if w)


def _clean_label(text: str) -> str:
    """
    Normalise an inferred label:
      - strip checkbox/dingbat glyphs
      - drop pure-underline placeholder runs (e.g. "______")
      - collapse internal whitespace
      - trim stray leading/trailing punctuation/colons

    Returns "" when nothing meaningful remains (caller should fall back to the
    raw field name).
    """
    if not text:
        return ""
    text = _strip_glyphs(text)
    text = " ".join(text.split())
    # Pure underline / dash placeholder → no real label
    if text and set(text) <= {"_", "-", ".", " "}:
        return ""
    return text.strip(" :\t")


_YESNO_EXPORTS = {"yes", "no", "y", "n", "true", "false", "on", "off"}


def _is_yesno_export(value: str | None) -> bool:
    """True for a radio-option export value that reads as a plain yes/no-
    style answer (e.g. "yes", "No", "Y", "TRUE"). These are unambiguous and
    far more reliable than a geometry-derived option label whenever the
    printed text near the widget is corrupted (e.g. two overlapping text
    layers producing interleaved garbage like "MExepdeiccatetdio")."""
    if not value:
        return False
    return value.strip().lower() in _YESNO_EXPORTS


_GENERIC_EXPORTS = {
    "yes", "no", "y", "n", "true", "false", "on", "off", "none",
    "undefined", "checked", "unchecked", "selected", "choice", "value",
    # auto-generated placeholder export names some editors stamp onto
    # checkbox groups (e.g. an Adobe "Gender" group whose states are
    # "Maybe2"/"Sometimes26") — never a real printed option label.
    "maybe", "sometimes", "somewhat",
}


def _is_meaningful_export(value: str | None) -> bool:
    """True when a radio-option export value is itself a real, word-like OPTION
    LABEL (e.g. "Male", "Female", "Inpatient") rather than a generic on/off
    token ("On", "Yes", "1", "undefined_2", "Choice_3"). For groups whose /TU
    is only the shared group name ("Sex"), such an export is the cleanest
    per-option label — geometry tends to grab the group word or interleaved
    neighboring text."""
    if not value:
        return False
    v = value.strip()
    # Reject corrupt export states: C0/C1 control chars ("3\x855 weeks") or
    # non-Latin mojibake bytes ("Patient恠 Home"). A real option label on these
    # (English) forms is Latin-script + common punctuation; anything outside
    # that is a broken export we must not surface as a label.
    for ch in v:
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F:
            return False
        if o > 0x24F and not (0x2000 <= o <= 0x206F):
            return False
    low = v.lower()
    if low in _GENERIC_EXPORTS:
        return False
    # strip a trailing disambiguator ("_2", "3") viewers append to duplicates
    base = re.sub(r"[ _-]?\d+$", "", low).strip("_ -")
    if not base or base in _GENERIC_EXPORTS:
        return False
    if base.startswith("undefined") or base.startswith("choice"):
        return False
    letters = re.sub(r"[^a-z]", "", base)
    return len(letters) >= 2


def _is_question_like_name(name: str) -> bool:
    """True when a raw AcroForm field /T reads like an actual printed
    question/prompt rather than a generic system name (e.g. "Check Box12",
    "F[0].P1[0].Btn3[0]"). Used to trust the field name as group context
    only when it plausibly IS the real printed text (some forms embed the
    exact question as the field's own name)."""
    if not name or "[" in name or "." in name:
        return False
    words = name.split()
    if name.rstrip().endswith("?") and len(words) >= 3:
        return True
    return len(words) >= 4 and len(name) >= 20


def _norm_token(text: str) -> str:
    """Lowercase and strip everything but letters/digits, for loose
    word-by-word matching (e.g. "Long?_______________" -> "long")."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _norm_dashes(text: str) -> str:
    """Fold the various Unicode dash glyphs (figure/en/em/horizontal-bar/minus)
    to a single em dash so the SAME section title read from different page
    chunks (e.g. "Section V – Services…" vs "Section V ― Services…") collapses
    to one string instead of fragmenting section grouping. ASCII hyphen '-' is
    left untouched (it appears inside legitimate labels)."""
    if not text:
        return text
    return re.sub(r"[\u2012\u2013\u2014\u2015\u2212]", "\u2014", text)


def _norm_section(text: str) -> str:
    """Canonicalize a section string so the SAME section reads identically for
    every field in it. Collapses embedded newlines/extra spaces and, when an
    instruction line got prepended (e.g. "Complete the following section …\\n
    Section A: …"), trims back to the real "Section …" header so it matches the
    clean copy other fields received."""
    if not text:
        return text
    t = re.sub(r"\s+", " ", str(text).replace("\n", " ")).strip()
    m = re.search(r"(Section\s+[A-Za-z0-9]+\b.*)", t)
    return m.group(1).strip() if m else t


_CONFIDENCE_BASE = {
    "acroform-tu": 0.90,   # the PDF's own authored tooltip — authoritative
    "gemini": 0.80,
    "vision": 0.80,
    "table-header": 0.80,  # inside a real bordered grid
    "name-match": 0.80,    # field name matched printed text verbatim
    "export": 0.78,        # radio option's own export value
    "geometry": 0.70,      # side-scan of printed text
    "ocr": 0.65,
    "cell-group": 0.65,    # label above, but not a confirmed grid cell
    "name": 0.30,          # fell back to the raw field name (no real label)
}


def _tokset(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _compute_confidence(
    *,
    label: str | None,
    source: str | None,
    tu: str | None,
    section: str | None,
    base_label: str | None,
    is_dup: bool,
) -> float:
    """Score 0-1 that a field's label is correct, from cross-source agreement
    plus quality signals. Used purely as a review-triage hint — never changes
    the label itself.

    Signals:
      * base score by ``source`` provenance (``/TU`` > table-header > geometry
        > name);
      * AGREEMENT with the field's own ``/TU`` tooltip (authoritative): a final
        label that shares a word with /TU is boosted; an AI label that shares
        NOTHING with a present /TU is a conflict and is penalised;
      * agreement between the AI label and the geometry ``base_label`` (a
        second independent read) gives a small boost;
      * penalties for a missing section, a truncated caption, or a label that
        duplicates another field's in the same section.
    """
    if not label or not str(label).strip():
        return 0.05
    src = source or ""
    score = _CONFIDENCE_BASE.get(src, 0.60)

    lab_toks = _tokset(label)
    tu_toks = _tokset(tu)
    if tu_toks and lab_toks:
        if tu_toks & lab_toks:
            score = max(score, 0.90)          # agrees with authoritative /TU
        elif src in ("gemini", "vision"):
            score = min(score, 0.55)          # AI overrode /TU with something unrelated

    if base_label and src in ("gemini", "vision"):
        if lab_toks & _tokset(base_label):    # AI ⇄ geometry agree
            score = min(0.97, score + 0.05)

    if not section:
        score -= 0.10
    # A label that still opens with its own section string is a leaked
    # section/instruction prefix ("Section A: … - No"), not a real caption.
    if section and str(label).strip().lower().startswith(str(section).strip().lower()):
        score = min(score, 0.45)
    if _looks_truncated(label):
        score = min(score, 0.40)
    if is_dup:
        score -= 0.15

    return round(max(0.05, min(0.99, score)), 2)


def _looks_truncated(text: str) -> bool:
    """Heuristic for a clipped caption Gemini sometimes returns when a widget
    box overlaps the printed label (e.g. "Specialty:" -> "cialty:", "Name:" ->
    "ne:", "Address:" -> "dress:", or bare "#:" / "/"). Used only to prefer a
    real geometry/`/TU` label when one exists — never to blank a field."""
    s = (text or "").strip()
    if len(s) <= 2:
        return True
    core = s.rstrip(":").strip()
    if not core:
        return True
    # No alphanumerics at all ("/", "#:", "—").
    if not any(c.isalnum() for c in s):
        return True
    # Mid-word clip: starts lowercase AND is a colon-terminated prompt fragment.
    if core[0].islower() and s.endswith(":"):
        return True
    return False


def _find_name_matched_label(field_name: str, f_top: float, words: list[dict]) -> str | None:
    """Some forms mis-place a text field's rect a few pixels into the WRONG
    printed row (a PDF-authoring quirk, not a text-extraction bug) — e.g. a
    "How Long" field's box drawn slightly overlapping the row above its own
    "How Long?___" printed prompt, so the normal same-row scan captures an
    unrelated neighboring sentence instead.

    When the field's own AcroForm name is a real (non-generic) phrase, look
    for that EXACT sequence of words printed anywhere within a nearby
    vertical band and, if found, trust it over a same-row match to
    unrelated text — this is a strong, low-risk signal since it requires an
    exact word-for-word match, not just position.
    """
    name_tokens = [t for t in (_norm_token(w) for w in field_name.split()) if t]
    if not name_tokens:
        return None
    # Require a real, specific phrase — not a single short/generic word
    # ("Name", "Date") that could coincidentally match unrelated text.
    if len(name_tokens) < 2 and len(name_tokens[0]) < 6:
        return None

    NAME_MATCH_WINDOW = 80  # px: how far from the field's own (mis-placed) row to search
    lines: dict[int, list[dict]] = defaultdict(list)
    for w in words:
        if abs(w["top"] - f_top) <= NAME_MATCH_WINDOW:
            lines[round(w["top"] / 4) * 4].append(w)

    best: str | None = None
    best_dist: float | None = None
    n = len(name_tokens)
    for bucket, line_ws in lines.items():
        line_ws = sorted(line_ws, key=lambda w: w["x0"])
        toks = [_norm_token(w["text"]) for w in line_ws]
        for j in range(len(toks) - n + 1):
            if toks[j:j + n] != name_tokens:
                continue
            dist = abs(bucket - f_top)
            if best is not None and dist >= best_dist:
                break
            raw = " ".join(w["text"] for w in line_ws[j:j + n])
            # Drop a trailing underline/blank run glued to the last word
            # (e.g. "Long?_______________" -> "Long?").
            raw = re.sub(r"[_\-.]{3,}\s*$", "", raw).strip()
            cleaned = _clean_label(raw)
            if cleaned:
                best, best_dist = cleaned, dist
            break
    return best


def _humanize_field_name(name: str) -> str:
    """
    Convert a raw AcroForm field name into a readable label when geometry
    label detection has failed.

    Transformations applied in order:
      1. Take the leaf of a dotted path  ("F[0].P1[0].FirstName" -> "FirstName")
      2. Drop array-index suffixes        ("Name[0]" -> "Name")
      3. Replace _ and - with spaces
      4. Split camelCase                  ("FirstName" -> "First Name")
      5. Insert space between a letter and a glued digit ("Code2" -> "Code 2")
      6. Insert space between a digit and a letter       ("2ndLine" -> "2nd Line")
      7. Collapse whitespace and return

    Examples
    --------
    "Administrative Code2"  -> "Administrative Code 2"
    "First Name_2"          -> "First Name 2"
    "ICD10"                 -> "ICD 10"
    "Quantity of Ingredient 3" -> "Quantity of Ingredient 3"  (unchanged)
    """
    if not name:
        return name
    # Leaf of dotted path
    base = name.split(".")[-1]
    # Drop [0]-style array indices
    base = re.sub(r"\[\d+\]", "", base)
    # Underscores/dashes → spaces
    s = base.replace("_", " ").replace("-", " ")
    # Split camelCase: lowercase → uppercase boundary
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    # Space between letter and glued digit ("Code2" → "Code 2")
    s = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)
    # Space between digit and letter ("2nd" stays, "2Name" → "2 Name")
    s = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", s)
    # Collapse repeated whitespace
    s = " ".join(s.split())
    return s or name


# ---------------------------------------------------------------------------
# Prompt context builder (pluggable, opt-in coordinate enhancement)
# ---------------------------------------------------------------------------

def _build_labeled_fields(
    fields_info: list[dict],
    field_labels: dict[str, str],
) -> list[dict]:
    """
    Build the list of field descriptors sent to the LLM.

    When ``settings.AI_USE_COORDINATES`` is False (default) this is identical
    to the previous behaviour: {field_name, type, label}.

    When True it additionally includes coarse positional context:
      - page   (0-based)
      - x_band ("left" | "center" | "right" — thirds of page width)
      - y_band ("top" | "middle" | "bottom" — thirds of page height)

    The bands let the LLM reason that e.g. "Patient Phone" at the bottom of
    page 0 is different from "Physician Phone" at the top, even though both
    have a label containing "Phone".

    Page width/height are not available here (we only have PDF-point coords
    from pdfplumber), so we use percentile ranks within the page instead.
    """
    use_coords = settings.AI_USE_COORDINATES

    if not use_coords:
        return [
            {
                "field_name": f["name"],
                "type": "textbox" if "/Tx" in f["type"] else "checkbox",
                "label": field_labels.get(f["name"], f["name"]),
            }
            for f in fields_info
        ]

    # Compute per-page percentile ranks for coarse band assignment
    # Group x0 and y values by page
    pages: dict[int, list[dict]] = {}
    for f in fields_info:
        pages.setdefault(f["page"], []).append(f)

    page_ranges: dict[int, dict] = {}
    for pg, pg_fields in pages.items():
        xs = [f["x0"] for f in pg_fields]
        ys = [f["y"] for f in pg_fields]
        page_ranges[pg] = {
            "x_min": min(xs), "x_max": max(xs) or 1,
            "y_min": min(ys), "y_max": max(ys) or 1,
        }

    def _band(val: float, lo: float, hi: float) -> str:
        rng = hi - lo or 1.0
        pct = (val - lo) / rng
        if pct < 0.33:
            return "low"
        if pct < 0.67:
            return "mid"
        return "high"

    result = []
    for f in fields_info:
        ftype = "textbox" if "/Tx" in f["type"] else "checkbox"
        label = field_labels.get(f["name"], f["name"])
        pr = page_ranges.get(f["page"], {"x_min": 0, "x_max": 1, "y_min": 0, "y_max": 1})
        # x_band: left/center/right on the page (pdfplumber x0)
        x_band = _band(f["x0"], pr["x_min"], pr["x_max"])
        # y_band: top/middle/bottom — note pdfplumber y=0 is page top
        y_band = _band(f["y"], pr["y_min"], pr["y_max"])
        row: dict = {
            "field_name": f["name"],
            "type": ftype,
            "label": label,
            "page": f["page"],
            "position": f"x:{x_band} y:{y_band}",
        }
        result.append(row)
    return result


def _structured_key_paths(obj, prefix: str = "") -> list[str]:
    """Flatten a nested structured record to a sorted list of KEY PATHS only.

    Values are deliberately dropped — only the schema (dotted key paths, with
    ``[]`` for list nesting) is returned so it can be shown to the AI for
    disambiguation without leaking any PHI. Canonical records key by fixed
    schema field names, so the paths themselves carry no patient data.
    """
    paths: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                paths.extend(_structured_key_paths(v, p))
            else:
                paths.append(p)
    elif isinstance(obj, list):
        for item in obj:
            paths.extend(_structured_key_paths(item, prefix + "[]"))
    return sorted(set(paths))


class VisionService:
    """AI-powered PDF field mapping and filling"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._cache = TemplateCache()          # deprecated value cache (PHI); off by default
        self._map_cache = FieldMapCache()      # PHI-free schema mapping cache
        self._label_cache = LabelCache()
        # Canonical fork ("Call 4"): field → fixed canonical path, PHI-free + cached
        self._canonical_service = CanonicalFieldService(api_key, base_url, model)

    # ------------------------------------------------------------------
    # Step 1: Extract fields with full bounding box + page info
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_widget_name_and_type(annot) -> tuple[Optional[str], Optional[str]]:
        """
        Resolve a widget annotation's field name (/T) and field type (/FT),
        walking up the /Parent chain when the widget itself doesn't carry
        them directly.

        Some AcroForms put the widget's /T only on the /Parent field object
        (a single terminal field with one kid widget) — without this walk
        those widgets are silently dropped, and any fields whose values were
        read fine (via pypdf's own field-tree walk) show up as unlabeled
        "null" rows in extraction output.
        """
        name_obj = annot.get("/T")
        ft_obj = annot.get("/FT")
        node = annot
        seen: set = set()
        while (name_obj is None or ft_obj is None) and "/Parent" in node:
            parent_ref = node["/Parent"]
            pid = id(parent_ref)
            if pid in seen:
                break
            seen.add(pid)
            node = parent_ref.get_object()
            if name_obj is None:
                name_obj = node.get("/T")
            if ft_obj is None:
                ft_obj = node.get("/FT")
        return (str(name_obj) if name_obj is not None else None), (
            str(ft_obj) if ft_obj is not None else None
        )

    @staticmethod
    def _resolve_qualified_name(annot) -> Optional[str]:
        """Build the widget's FULLY-QUALIFIED field name — every ancestor /T
        joined top-to-bottom with '.', exactly as pypdf's ``get_fields()`` keys
        them (e.g. parent /T "Member Info T" + widget /T "0" -> "Member Info
        T.0").

        The plain name (see _resolve_widget_name_and_type) is only the nearest
        /T ("0"), which is AMBIGUOUS: this form restarts leaf numbering inside
        each parent, so "0" names five unrelated fields. The qualified name is
        unique and lets downstream value-readers (ExtractionService) line labels
        up 1:1 with the /V values instead of guessing by leaf name."""
        parts: list[str] = []
        node = annot
        seen: set = set()
        while node is not None:
            t = node.get("/T")
            if t is not None:
                parts.append(str(t))
            if "/Parent" not in node:
                break
            parent_ref = node["/Parent"]
            pid = id(parent_ref)
            if pid in seen:
                break
            seen.add(pid)
            node = parent_ref.get_object()
        if not parts:
            return None
        parts.reverse()
        return ".".join(parts)

    @staticmethod
    def _resolve_field_flags(annot) -> int:
        """Resolve a widget's /Ff (field flags) bitmask, walking up the
        /Parent chain when the flags live on the parent field object (as they
        do for radio groups and many button fields). Returns 0 when absent."""
        node = annot
        seen: set = set()
        while node is not None:
            ff = node.get("/Ff")
            if ff is not None:
                try:
                    return int(ff)
                except (TypeError, ValueError):
                    return 0
            if "/Parent" not in node:
                break
            parent_ref = node["/Parent"]
            pid = id(parent_ref)
            if pid in seen:
                break
            seen.add(pid)
            node = parent_ref.get_object()
        return 0

    @staticmethod
    def _resolve_widget_tu(annot) -> Optional[str]:
        """Resolve a widget's /TU (alternate field name), walking up the
        /Parent chain. /TU is the accessibility/tooltip label the form author
        attaches to a field and is, when present, an AUTHORITATIVE per-widget
        caption — e.g. a form that names every box "0","1","2"… (useless as a
        label, and duplicated across sections) can still carry /TU="Primary
        ICD code" on the exact widget. Returns the raw string or None."""
        node = annot
        seen: set = set()
        tu = node.get("/TU")
        while tu is None and "/Parent" in node:
            parent_ref = node["/Parent"]
            pid = id(parent_ref)
            if pid in seen:
                break
            seen.add(pid)
            node = parent_ref.get_object()
            tu = node.get("/TU")
        return str(tu) if tu is not None else None

    @staticmethod
    def _clean_tu(tu: Optional[str]) -> Optional[str]:
        """Return a usable label from a raw /TU, or None if it's absent or
        generic boilerplate (a bare "Text Field 3", "undefined", etc. carries
        no more meaning than the field name and should defer to geometry)."""
        if not tu:
            return None
        s = " ".join(str(tu).split()).strip()
        # Drop a trailing prompt separator (":" / "…:") so /TU captions read
        # like the geometry labels ("Phone" not "Phone:").
        s = s.rstrip().rstrip(":").rstrip()
        if len(s) < 2 or len(s) > 120:
            return None
        if not any(c.isalpha() for c in s):
            return None
        low = s.lower()
        if low in ("undefined", "text", "field", "checkbox", "check box", "value"):
            return None
        if re.fullmatch(r"(text\s*field|check\s*box|field|button|radio)\s*\d*", low):
            return None
        return s

    @staticmethod
    def _widget_key(f: dict) -> str:
        """Storage key for a widget's resolved label.

        Normally the AcroForm field name. For radio-group option widgets
        (same parent field name, distinct export values — e.g. a Yes/No
        radio) the key is name-scoped by export value so the two options
        keep their own labels instead of collapsing to the first-resolved
        one.

        When a widget carries a meaningful /TU, the key is name-scoped by
        that /TU too. Some forms reuse ONE field name ("0","1","2"…) for
        many unrelated widgets across different sections (technically a
        single shared field, but authored as distinct captions via /TU).
        Keying by name alone would collapse them all to the first-resolved
        label/section/table (so the "Primary ICD code" box inherits "Member
        name"); adding /TU keeps genuinely-distinct widgets separate while
        still de-duping true mirror widgets (same name AND same /TU, e.g. a
        header field repeated on every page).
        """
        if f.get("_radio_group") and f.get("export_value"):
            return f"{f['name']}\x1f{f['export_value']}"
        if f.get("_acro") and f.get("tu"):
            return f"{f['name']}\x1f{f['tu']}"
        return f["name"]

    def _get_fields_with_coords(self, pdf_path: str) -> list[dict]:
        """
        Read every AcroForm annotation and return a list of dicts with:
          name, type, page, x0, x1, x, y (top), y_bottom (bottom-of-widget),
          all in pdfplumber's top-origin coordinate system.
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
                        name, ft = self._resolve_widget_name_and_type(annot)
                        if name is None:
                            continue
                        ft = ft or "/Tx"
                        # Skip PUSHBUTTONS — /Btn fields with the pushbutton
                        # flag set (/Ff bit 17, 0x10000). These are action
                        # controls (page-navigation first/prev/next/last
                        # buttons, "Submit"/"Reset"/"Print" triggers), NOT
                        # data-entry widgets: they hold no value and carry a
                        # /GoTo|/Named|/SubmitForm|/JavaScript action instead
                        # of an /AP export state. Some documents (e.g. paged
                        # provider manuals) embed dozens of these and nothing
                        # else, which would otherwise surface as bogus
                        # "checkbox" fields. Real checkboxes/radios are also
                        # /Btn but WITHOUT this flag, so they're unaffected.
                        field_flags = self._resolve_field_flags(annot)
                        if "/Btn" in ft and (field_flags & 0x10000):
                            continue
                        # /Ff bit 13 (0x1000) = multiline text. Authored as a
                        # textarea, so it holds a free-text narrative rather
                        # than an identity value the canonical catalog covers.
                        multiline = bool("/Tx" in ft and (field_flags & 0x1000))
                        rect = annot.get("/Rect")
                        if rect:
                            x0 = float(rect[0])
                            y0 = float(rect[1])
                            x1 = float(rect[2])
                            y1 = float(rect[3])
                            # Convert PDF bottom-origin → top-origin (matches pdfplumber)
                            field_top = page_height - max(y0, y1)
                            field_bottom = page_height - min(y0, y1)
                        else:
                            x0 = x1 = field_top = field_bottom = 0.0
                        # Export value of this specific widget (the /AP /N
                        # appearance state that is not "/Off"). For a radio
                        # group, each option widget carries a DISTINCT export
                        # (e.g. "Yes" / "No_2") even though every widget shares
                        # the same parent field /T — this lets us keep the two
                        # options as separate rows instead of collapsing them.
                        export_value = None
                        try:
                            ap = annot.get("/AP")
                            n = ap.get_object().get("/N") if ap else None
                            if n:
                                for k in n.get_object().keys():
                                    if k != "/Off":
                                        export_value = str(k).lstrip("/")
                                        break
                        except Exception:
                            export_value = None
                        tu = self._clean_tu(self._resolve_widget_tu(annot))
                        qualified_name = self._resolve_qualified_name(annot)
                        fields_info.append({
                            "name": name,
                            "qualified_name": qualified_name,
                            "type": ft,
                            "page": page_num,
                            "x0": round(x0),
                            "x1": round(x1),
                            "x": round((x0 + x1) / 2),   # center (for sorting)
                            "y": round(field_top),         # distance from top
                            "y_bottom": round(field_bottom),
                            "export_value": export_value,
                            "tu": tu,
                            "multiline": multiline,
                            # Mark this as a REAL AcroForm widget read straight
                            # from the PDF — so its /TU provably belongs to this
                            # exact box. The OpenCV/VLM engines re-detect boxes
                            # visually and IoU-bind them to widgets; a rebound
                            # box's geometry no longer matches its /TU, so /TU
                            # must NOT be trusted there. This flag gates that.
                            "_acro": True,
                        })
                    except Exception:
                        continue
            # Mark radio-group option widgets: a field name shared by 2+ button
            # widgets with DISTINCT export values (e.g. a Yes/No radio). These
            # are keyed per-option downstream so each option keeps its own
            # label, rather than both inheriting the first-resolved one.
            btn_exports: dict[str, set] = {}
            for f in fields_info:
                if "/Btn" in f["type"] and f.get("export_value"):
                    btn_exports.setdefault(f["name"], set()).add(f["export_value"])
            for f in fields_info:
                if "/Btn" in f["type"] and len(btn_exports.get(f["name"], ())) >= 2:
                    f["_radio_group"] = True
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
    ) -> dict[str, dict]:
        """
        Lane-aware label extraction using pdfplumber word positions.

        Algorithm per field:
          1. Section detection: identify section-header anchors per page (text
             lines matching "Section N" or all-caps band) so each field can be
             tagged with the section it belongs to.
          2. Side scan (same row, matched by vertical overlap with the widget's
             own box, not just its top edge — see _word_on_row):
             - textbox  → look LEFT, stop at nearest neighbor field edge (lane).
             - checkbox → look RIGHT up to next field; extract only the FIRST
               option run, then look LEFT for a group qualifier ("Sex").
          3. Table-header fallback (if side scan found nothing):
             Look ABOVE (4-50px) for the nearest printed row; optionally join
             with a wider super-header one band higher (two-level tables).
          4. Fall back to humanized field name.
          5. Duplicate-label disambiguation: prefix section when no row context.

        Returns dict[field_name -> {"label": str, "source": str, "section": str|None}]
        source: "geometry" | "table-header" | "cell-group" | "name"
          - "table-header": the above-scan label sits in a cell fully
            enclosed by rulings on all 4 sides (both a column AND a row
            boundary detected) — i.e. an actual grid of rows and columns.
          - "cell-group": the above-scan found a printed label, but the
            field is NOT inside a fully-bordered grid cell (no confirmed
            row+column structure) — e.g. a plain label floated above a
            blank line, or a shaded/bordered block that isn't a real table.
        """
        import pdfplumber

        # Patterns that identify section headers.
        # Anchored at the start so body text like "… (if different from Section I) …"
        # is NOT treated as a section header. Accepts digit/roman-numeral AND
        # single-letter section labels ("Section A", "Section B"), plus "Part N".
        SECTION_RE = re.compile(r"(?i)^(section|part)\s+([0-9]+|[ivxlcm]+|[a-z])\b")

        MAX_LEFT_DIST    = 200  # px: outer left bound (lane boundary is tighter)
        MAX_RIGHT_DIST   = 450  # px: for checkboxes
        MAX_LABEL_WORDS  = 8    # rightmost N words left of field (the label; textboxes)
        CHECKBOX_LABEL_WORDS = 4  # word cap for checkbox option-run chaining
        # A checkbox option run ends before a word that has its OWN checkbox
        # widget sitting just to its left — that word starts a SEPARATE option.
        CHECKBOX_OPT_BOX_LEFT = 18   # px: how far left of a word its box may sit
        CHECKBOX_OPT_BOX_BAND = 30   # px: vertical slack (box may be a row off)
        CHECKBOX_X_THRESHOLD = 80
        ROW_PAD = 2  # px: extra slack applied on top of the widget's own box

        def _row_overlap(top1: float, bot1: float, top2: float, bot2: float) -> bool:
            """True if two vertical ranges (widget/word boxes) overlap, with a
            small pad. Falls back to a 1px-tall band if bot <= top (degenerate
            box, e.g. missing /Rect)."""
            if bot1 <= top1:
                bot1 = top1 + 1
            if bot2 <= top2:
                bot2 = top2 + 1
            return max(top1, top2) <= min(bot1, bot2) + ROW_PAD

        def _word_on_row(w: dict, y_top: float, y_bottom: float) -> bool:
            """True if word w's vertical center falls within a field's row
            band [y_top, y_bottom] (± ROW_PAD). Replaces the old "word top
            within 7px of field top" test, which broke on tall widgets whose
            printed label sits well below the widget's top edge."""
            b = y_bottom if y_bottom > y_top else y_top + 1
            w_center = (w["top"] + w.get("bottom", w["top"])) / 2.0
            return (y_top - ROW_PAD) <= w_center <= (b + ROW_PAD)

        def _fields_same_row(f1: dict, f2: dict) -> bool:
            return _row_overlap(
                f1["y"], f1.get("y_bottom", f1["y"]),
                f2["y"], f2.get("y_bottom", f2["y"]),
            )

        from collections import defaultdict
        page_fields: dict[int, list[dict]] = defaultdict(list)
        for f in fields_info:
            page_fields[f["page"]].append(f)

        result: dict[str, dict] = {}

        try:
            with pdfplumber.open(pdf_path) as pdf:
                page_words: dict[int, list[dict]] = {}
                page_rects: dict[int, list[dict]] = {}
                page_lines: dict[int, list[dict]] = {}
                page_widths: dict[int, float] = {}
                page_heights: dict[int, float] = {}
                # Per-page set of 4px "top" buckets that read as SECTION
                # HEADINGS by typography: predominantly BOLD *and* set in a
                # font larger than the page's body text. Some forms mark
                # section headers by weight/size rather than ALL-CAPS /
                # "Section N" / a shaded bar (e.g. the aetna CO Rx-PA form's
                # "Patient Information:" | "Prescribing Provider Information:"
                # row, 11pt bold over 9pt body). The size gate is essential:
                # WITHOUT it, ordinary bold field labels whose input box sits
                # BELOW them ("First Name:", "Member's Last Name:") — same size
                # as body — would be misread as sections. extract_words() drops
                # font info, so derive this once from the raw chars without
                # disturbing the global word extraction downstream depends on.
                page_bold_buckets: dict[int, set] = {}
                # Per-page "Section N"/"Part N" headers recovered by DE-INTER-
                # LEAVING overlapping text runs by font. Some forms draw a
                # section header on the SAME baseline as a field-label row (e.g.
                # cigna's bold-italic "Section II — General Information" printed
                # over the regular "Issuer Name  Phone  Fax" row); extract_words
                # then weaves their characters into garbage ("ISsescuteior nN
                # IaIm —e") that SECTION_RE can't match, so those sections go
                # undetected and their fields inherit the previous section.
                page_font_section_anchors: dict[int, list] = {}
                for i, plumb_page in enumerate(pdf.pages):
                    page_words[i] = plumb_page.extract_words(
                        x_tolerance=3, y_tolerance=3, keep_blank_chars=False
                    )
                    _bold_tally: dict[int, list] = defaultdict(lambda: [0, 0, []])
                    _size_freq: dict[float, int] = defaultdict(int)
                    for _c in plumb_page.chars:
                        if not str(_c.get("text", "")).strip():
                            continue
                        _b = round(_c["top"] / 4) * 4
                        _sz = round(float(_c.get("size", 0) or 0), 1)
                        _bold_tally[_b][1] += 1
                        _bold_tally[_b][2].append(_sz)
                        _size_freq[_sz] += 1
                        if "bold" in str(_c.get("fontname", "")).lower():
                            _bold_tally[_b][0] += 1
                    # Body font size = the single most common char size.
                    _body_sz = max(_size_freq.items(), key=lambda kv: kv[1])[0] if _size_freq else 0.0
                    _hdr_buckets = set()
                    for _b, (_nb, _nt, _szs) in _bold_tally.items():
                        if _nt <= 0 or _nb / _nt < 0.6:
                            continue
                        _szs_sorted = sorted(_szs)
                        _med = _szs_sorted[len(_szs_sorted) // 2] if _szs_sorted else 0.0
                        if _body_sz and _med >= _body_sz * 1.12:
                            _hdr_buckets.add(_b)
                    page_bold_buckets[i] = _hdr_buckets

                    # De-interleave by (4px band, fontname) and keep ONLY clean
                    # "Section N"/"Part N" reconstructions — a narrow, high-
                    # precision signal that won't fire on ordinary body text.
                    _font_groups: dict[tuple, list] = defaultdict(list)
                    for _c in plumb_page.chars:
                        if not str(_c.get("text", "")).strip():
                            continue
                        _fg = (round(_c["top"] / 4) * 4, str(_c.get("fontname", "")))
                        _font_groups[_fg].append(_c)
                    _fsa: list = []
                    for _cs in _font_groups.values():
                        _cs.sort(key=lambda c: c["x0"])
                        _parts: list = []
                        _prev_x1 = None
                        for _c in _cs:
                            if _prev_x1 is not None and (_c["x0"] - _prev_x1) > 1.5:
                                _parts.append(" ")
                            _parts.append(_c["text"])
                            _prev_x1 = _c["x1"]
                        _txt = re.sub(r"\s+", " ", "".join(_parts)).strip()
                        if SECTION_RE.search(_txt):
                            _fsa.append((min(c["top"] for c in _cs), _txt))
                    page_font_section_anchors[i] = _fsa
                    page_rects[i] = plumb_page.rects
                    # Some forms draw their table grid as vector line STROKES
                    # (pdfplumber `.lines`, width/height == 0) rather than thin
                    # filled rects — e.g. the IBX ABA PA form's "Section D —
                    # Standardized assessments" grid. Harvest both so those
                    # rulings aren't invisible to table/cell detection.
                    page_lines[i] = plumb_page.lines
                    page_widths[i] = float(plumb_page.width)
                    page_heights[i] = float(plumb_page.height)

                # ── Part A0: build per-page cell-ruling grid from rects ──────
                # Bordered tables draw each cell wall as a separate thin rect
                # (pdfplumber decomposes overlapping fills into many small
                # slivers), so a single printed vertical/horizontal rule shows
                # up as dozens of adjacent rects sharing an x/y center. Cluster
                # them into named "rulings" so label/group scans can be
                # clamped to the cell a field actually sits in, instead of
                # bleeding into the next column/row via fixed pixel margins.
                RULE_THIN    = 3  # px: a rect this thin (or less) on one axis is a ruling, not a fill
                RULE_MIN_LEN = 6  # px: ignore tiny corner/anti-alias slivers

                def _cluster_and_merge(segs: list[tuple]) -> list[tuple]:
                    """segs: list of (coord, span_lo, span_hi). Groups entries
                    with near-equal `coord` (±2px) and merges their spans into
                    contiguous [lo, hi] runs. Returns list of
                    (coord, [(lo, hi), ...]) sorted by coord."""
                    if not segs:
                        return []
                    segs = sorted(segs, key=lambda s: s[0])
                    groups: list[list[tuple]] = [[segs[0]]]
                    for s in segs[1:]:
                        if s[0] - groups[-1][-1][0] <= 2.0:
                            groups[-1].append(s)
                        else:
                            groups.append([s])
                    out = []
                    for g in groups:
                        coord = sum(s[0] for s in g) / len(g)
                        spans = sorted((s[1], s[2]) for s in g)
                        merged: list[list[float]] = []
                        for lo, hi in spans:
                            if merged and lo <= merged[-1][1] + 1.5:
                                merged[-1][1] = max(merged[-1][1], hi)
                            else:
                                merged.append([lo, hi])
                        out.append((coord, [(lo, hi) for lo, hi in merged]))
                    return out

                page_v_rulings: dict[int, list[tuple]] = {}
                page_h_rulings: dict[int, list[tuple]] = {}
                for pnum in page_rects:
                    v_segs = []
                    h_segs = []
                    # Both thin filled rects AND vector line strokes describe
                    # rulings; _cluster_and_merge dedups by coordinate
                    # proximity, so a border drawn as BOTH (rect edge + line)
                    # collapses to one ruling rather than double-counting.
                    for r in list(page_rects.get(pnum, [])) + list(page_lines.get(pnum, [])):
                        w = r["x1"] - r["x0"]
                        h = r["bottom"] - r["top"]
                        if w <= RULE_THIN and h >= RULE_MIN_LEN:
                            v_segs.append(((r["x0"] + r["x1"]) / 2.0, r["top"], r["bottom"]))
                        if h <= RULE_THIN and w >= RULE_MIN_LEN:
                            h_segs.append(((r["top"] + r["bottom"]) / 2.0, r["x0"], r["x1"]))
                    page_v_rulings[pnum] = _cluster_and_merge(v_segs)
                    page_h_rulings[pnum] = _cluster_and_merge(h_segs)

                CELL_TOL = 1.5  # px: coverage/alignment slack when matching a field to its enclosing ruling

                def _cell_bounds(pnum: int, cx: float, y_top: float, y_bottom: float) -> dict:
                    """Return the printed table cell enclosing a point/row, as
                    far as rulings allow: {"x0", "x1", "top", "bottom"}, any of
                    which is None when that side isn't bounded by a detected
                    ruling (e.g. the form isn't a bordered grid there) —
                    callers only apply the sides they need, so partial results
                    are still useful and forms without rulings are unaffected.
                    """
                    y_c = (y_top + y_bottom) / 2.0

                    def _covers(spans, lo, hi):
                        return any(s_lo - CELL_TOL <= lo and hi <= s_hi + CELL_TOL for s_lo, s_hi in spans)

                    x0 = x1 = top = bottom = None
                    for vx, spans in page_v_rulings.get(pnum, []):
                        if not _covers(spans, y_top, y_bottom):
                            continue
                        if vx <= cx + CELL_TOL:
                            x0 = vx
                        elif x1 is None:
                            x1 = vx
                    for hy, spans in page_h_rulings.get(pnum, []):
                        if not _covers(spans, cx, cx):
                            continue
                        if hy <= y_c + CELL_TOL:
                            top = hy
                        elif bottom is None:
                            bottom = hy
                    return {"x0": x0, "x1": x1, "top": top, "bottom": bottom}

                # A single bordered label box (e.g. "Issuer Name", "DOB") has
                # rulings on all 4 sides too, but that alone doesn't make it
                # part of a real TABLE — a genuine table has column dividers
                # that repeat down MULTIPLE rows. The signature that reliably
                # tells them apart is that a true column divider is CROSSED by
                # several horizontal rulings (it borders a stack of rows),
                # while an isolated field's own divider only runs the height
                # of its one row (crossed by just that row's top + bottom).
                #
                # (An earlier version tried "connected components over the
                # full ruling-intersection graph", but that over-connects on
                # forms — like this TDI PA form — that draw a page-spanning
                # left/right margin rule plus a full-width separator under
                # EVERY field row: literally any two rulings anywhere on the
                # page end up transitively "connected" through that margin +
                # separator ladder, collapsing the whole page into one fake
                # table. Counting each vertical's OWN horizontal crossings has
                # no such failure mode — there is no transitivity, and the
                # later ">= 3 distinct verticals" gate still requires a real
                # interior column divider, so a margin+separator ladder with
                # no interior column is correctly rejected.
                #
                # A span-LENGTH bar ("divider >= 4 × median row height") was
                # tried before this, but it is not scale-free: it silently
                # misses SHORT tables whose dividers span only ~2 rows (e.g.
                # the IBX form's 3-row Section D assessment grid). Crossing
                # count is scale-independent and detects those too.
                MIN_ROW_CROSSINGS = 3  # top border + >= 1 interior rule + bottom border => spans >= 2 rows
                MULTIROW_FLOOR = 40.0   # px: "tall divider" floor used by the colon-header column-boundary
                                        # snap below to distinguish a real multi-row cell wall from a
                                        # one-row separator when placing a column boundary.

                def _detect_table_regions(pnum: int) -> list[dict]:
                    all_v = page_v_rulings.get(pnum, [])
                    h_rulings = page_h_rulings.get(pnum, [])
                    if len(all_v) < 2 or len(h_rulings) < 3:
                        return []

                    h_ys = sorted(hy for hy, _ in h_rulings)

                    def _row_crossings(vx: float, lo: float, hi: float) -> int:
                        """How many horizontal rulings actually cross this
                        vertical span (within its own y-range AND overlapping
                        its x)."""
                        n = 0
                        for hy, hspans in h_rulings:
                            if not (lo - CELL_TOL <= hy <= hi + CELL_TOL):
                                continue
                            if any(hx_lo - CELL_TOL <= vx <= hx_hi + CELL_TOL for hx_lo, hx_hi in hspans):
                                n += 1
                        return n

                    # Multi-row verticals (each crosses >= MIN_ROW_CROSSINGS
                    # horizontal rulings, i.e. borders >= 2 stacked rows).
                    multirow = [
                        (vx, lo, hi) for vx, spans in all_v for lo, hi in spans
                        if _row_crossings(vx, lo, hi) >= MIN_ROW_CROSSINGS
                    ]
                    if len(multirow) < 2:
                        return []

                    # Cluster on INTERIOR column dividers ONLY (not the page's
                    # left/right margin rules). A page-border line spans the
                    # WHOLE page and crosses every field-row separator on it,
                    # so if it were allowed into the Y-overlap clustering it
                    # would bridge otherwise-separate row bands into one giant
                    # bogus "table" (e.g. the IBX form's margins span 184→660,
                    # bridging Section D's 3-row grid with an unrelated grid
                    # 200px below it, and on plain full-border forms it would
                    # collapse the entire page). Margins are re-added purely as
                    # the outer edge in the widen-out step below, after a
                    # region has already been confirmed by its interior grid.
                    page_w = page_widths.get(pnum, 612.0)
                    margin_lo = page_w * 0.06
                    margin_hi = page_w * 0.94

                    def _is_margin(vx: float) -> bool:
                        return vx <= margin_lo or vx >= margin_hi

                    # A page/box OUTER frame is frequently drawn as SEVERAL
                    # parallel strokes a few px apart (a thick or double/triple
                    # border). Those inner strokes sit just INSIDE the 6% margin
                    # band, so they escape _is_margin yet still span the whole
                    # form — and, exactly like a true margin, would bridge every
                    # row band into one giant bogus table (e.g. the aetna CO
                    # Rx-PA form draws its left border as strokes at x≈33/40/47
                    # and its ONLY real column divider — the center Patient |
                    # Prescriber split — spans just the info block; without this
                    # the whole 49-field form collapses into one table). Treat
                    # any multi-row vertical within BORDER_STROKE_TOL of the
                    # page's leftmost/rightmost multi-row vertical as frame, not
                    # an interior column divider. They are still re-added as the
                    # table's outer edge via the widen-out step below.
                    BORDER_STROKE_TOL = 16.0
                    _mr_xs = [vx for vx, _lo, _hi in multirow]
                    _left_edge = min(_mr_xs)
                    _right_edge = max(_mr_xs)

                    def _is_frame(vx: float) -> bool:
                        return (
                            _is_margin(vx)
                            or (vx - _left_edge) <= BORDER_STROKE_TOL
                            or (_right_edge - vx) <= BORDER_STROKE_TOL
                        )

                    interior = sorted(
                        (s for s in multirow if not _is_frame(s[0])),
                        key=lambda s: s[1],
                    )
                    if not interior:
                        return []

                    # Cluster interior dividers by Y-overlap: consecutive
                    # dividers that overlap (or nearly touch) describe the
                    # same table band.
                    clusters: list[list[tuple]] = []
                    cur_hi = None
                    for item in interior:
                        _vx, lo, hi = item
                        if clusters and lo <= cur_hi + CELL_TOL:
                            clusters[-1].append(item)
                            cur_hi = max(cur_hi, hi)
                        else:
                            clusters.append([item])
                            cur_hi = hi

                    regions = []
                    for cluster in clusters:
                        # Band extent is defined by the INTERIOR dividers, so a
                        # tall margin can't stretch it past the real grid.
                        r_top = min(lo for _vx, lo, _hi in cluster)
                        r_bottom = max(hi for _vx, _lo, hi in cluster)
                        # Require an actual row split inside this band (not
                        # just one long field spanning 2 rows with nothing
                        # between) — at least 3 horizontal rulings fall inside.
                        n_inner_h = sum(1 for hy in h_ys if r_top - CELL_TOL <= hy <= r_bottom + CELL_TOL)
                        if n_inner_h < 3:
                            continue
                        r_x0 = min(vx for vx, _lo, _hi in cluster)
                        r_x1 = max(vx for vx, _lo, _hi in cluster)
                        # Widen out to ANY vertical (margins included) spanning
                        # this exact row-band, capturing the table's true outer
                        # edge — e.g. "Planned Service or Procedure", whose only
                        # left border is the page margin.
                        cover_xs = {round(vx, 1) for vx, _lo, _hi in cluster}
                        for vx, spans in all_v:
                            if any(lo - CELL_TOL <= r_top and r_bottom <= hi + CELL_TOL for lo, hi in spans):
                                cover_xs.add(round(vx, 1))
                                if vx < r_x0:
                                    r_x0 = vx
                                if vx > r_x1:
                                    r_x1 = vx
                        # A real >= 2-column grid needs >= 3 distinct verticals
                        # spanning the band: left + right outer border PLUS at
                        # least one interior column divider. A plain single-
                        # column bordered SECTION (e.g. a boxed "OHIO
                        # DEPARTMENT OF MEDICAID" block with row separators but
                        # no interior divider) has only its 2 outer borders and
                        # is correctly rejected here.
                        if len(cover_xs) < 3:
                            continue
                        regions.append({"x0": r_x0, "x1": r_x1, "top": r_top, "bottom": r_bottom})
                    return regions

                page_table_regions: dict[int, list[dict]] = {}
                page_table_names: dict[int, dict[int, str]] = {}
                _synthetic_table_counter = [0]
                # Checkbox rows with NO real header text (e.g. a bare
                # Inpatient/Outpatient/.../Day Surgery row) still need a
                # shared identity so co-located options are known to belong
                # together — cache keyed by the row's own member widgets so
                # every field in it resolves to the SAME synthetic id
                # regardless of processing order.
                synthetic_row_groups: dict[tuple, str] = {}
                _synthetic_group_counter = [0]
                # Same-row checkbox runs with a descriptive header printed to
                # the LEFT of the leftmost box (e.g. "Review Type Requested
                # [ ] Standard [ ] Urgent") instead of above/inline with each
                # option — cache keyed the same way as synthetic_row_groups so
                # every checkbox in the row resolves to the identical header
                # text regardless of which one is processed first.
                row_leadin_groups: dict[tuple, str | None] = {}
                # Composite split fields (a date "[MM] / [DD] / [YYYY]" etc.)
                # share ONE label across all their boxes. Resolve it once per
                # run (left-of-leftmost box first, else above the merged span)
                # and cache keyed by the run's member widgets so every box
                # reads the identical label regardless of processing order.
                composite_labels: dict[tuple, str | None] = {}

                def _table_region_for(pnum: int, cx: float, cy: float) -> tuple[int | None, dict | None]:
                    """Return (region_index, region) for the detected table
                    region enclosing this point, or (None, None)."""
                    regions = page_table_regions.get(pnum)
                    if regions is None:
                        regions = _detect_table_regions(pnum)
                        page_table_regions[pnum] = regions
                    for idx, r in enumerate(regions):
                        if (
                            r["x0"] - CELL_TOL <= cx <= r["x1"] + CELL_TOL
                            and r["top"] - CELL_TOL <= cy <= r["bottom"] + CELL_TOL
                        ):
                            return idx, r
                    return None, None

                def _table_name_for(pnum: int, idx: int, region: dict) -> str:
                    """Stable identity for a detected table region, so every
                    field inside it (all rows/columns) shares one `table`
                    value that autofill can group on. Prefer the table's own
                    section header (computed via _nearest_section, defined
                    below — safe to reference here since this is only ever
                    CALLED later, from the per-field loop, by which point
                    _nearest_section already exists in this closure); fall
                    back to a synthetic per-document "table_1", "table_2", …
                    when the table has no section header above it.
                    """
                    names = page_table_names.setdefault(pnum, {})
                    if idx in names:
                        return names[idx]
                    # A boxed table's own header row usually sits just INSIDE
                    # its top border, so it isn't strictly "above" the region.
                    # Prefer a column-overlapping section header within a header-
                    # row zone of the region top (e.g. the aetna CO form's bold
                    # "Prescribing Provider Information:" heading at the top of
                    # its info grid); otherwise fall back to the nearest header
                    # above the region (legacy behavior).
                    TABLE_HEADER_ZONE = 24.0
                    r_top = region["top"]
                    r_center = (region["x0"] + region["x1"]) / 2.0
                    sec = None
                    best_d = None
                    for (atop, atext, ax0, ax1) in page_sections.get(pnum, []):
                        if abs(atop - r_top) <= TABLE_HEADER_ZONE and ax0 - 2 <= r_center <= ax1 + 2:
                            d = abs(atop - r_top)
                            if best_d is None or d < best_d:
                                best_d = d
                                sec = atext
                    if sec is None:
                        sec = _nearest_section(pnum, region["top"], region["x0"], region["x1"])
                    if sec:
                        name = sec
                    else:
                        _synthetic_table_counter[0] += 1
                        name = f"table_{_synthetic_table_counter[0]}"
                    names[idx] = name
                    return name

                # ── Part A: detect section anchors per page ─────────────────
                # Group all words into text lines (cluster by 'top'), then find
                # lines that look like section headers.  Store as sorted list of
                # (top, header_text, x0, x1) per page so each field can look up
                # its nearest section above (optionally scoped to its column —
                # see _nearest_section).
                BAR_MIN_HEIGHT = 8    # px
                BAR_MAX_HEIGHT = 24   # px
                BAR_MIN_WIDTH  = 120  # px: skip thin rules/borders

                def _is_bar_fill(color) -> bool:
                    """True if a rect's fill color is a real (non-white/near-
                    white) shade, as used for section-header banners."""
                    if not color:
                        return False
                    try:
                        vals = color if isinstance(color, (list, tuple)) else [color]
                        return len(vals) > 0 and min(vals) < 0.92
                    except Exception:
                        return False

                # A run of text starting with a section enumerator
                # ("III. ", "IV) ", "2. ") — used only to decide whether a
                # single row holds TWO side-by-side section headers.
                # The char after the "." may be a SPACE ("III. PROVIDER") or
                # glued directly to the header word with no space at all
                # ("I.PROVIDER", "II.MEMBER") — pdfplumber sometimes extracts
                # the enumerator and first word as a single token. Requiring a
                # letter (not just any \S) after an optional space avoids
                # matching decimals like "3.5 mg".
                ENUM_RE = re.compile(r"^\s*(?:[ivxlcm]{1,7}|[0-9]{1,3})[.)]\s*[A-Za-z]", re.I)
                SECTION_RUN_GAP = 45  # px: gap that splits side-by-side headers on one row

                def _classify_header(text: str) -> dict | None:
                    """Judge whether a text line/run is a section header.
                    Returns {"cleaned", "core", "sec", "cap"} or None."""
                    cleaned = _clean_label(text) or text.strip()
                    if not cleaned:
                        return None
                    is_section_match = bool(SECTION_RE.search(cleaned))
                    # Accept short ALL-CAPS lines as section anchors (e.g.
                    # "PATIENT INFORMATION", "CLINICAL / MEDICATION INFORMATION",
                    # "PHYSICIAN INFORMATION (needed for mailing notification...)").
                    # Strip a trailing lowercase parenthetical first so the
                    # banner test only judges the real header text, and allow
                    # separators (/, &, -, comma, period) that don't affect
                    # case. Cap at 60 chars (post-strip) to exclude long
                    # form-title banners like
                    # "PRESCRIPTION DRUG PRIOR AUTHORIZATION … FORM".
                    stripped = cleaned.strip(" :")
                    core = re.sub(r"\s*\([^)]*\)\s*$", "", stripped).strip(" :")
                    letters_only = re.sub(r"[^A-Za-z]", "", core)
                    # Reject field-cell text that happens to be all-caps
                    # only because it has no lowercase letters at all
                    # (e.g. "*DOB(MM/DD/YYYY): / /", "NPI/TIN#: NPI/TIN#")
                    # — real banners are letter-dominated; these are
                    # punctuation/placeholder-heavy.
                    alpha_ratio = len(letters_only) / max(1, len(core))
                    is_junky_header = ("#" in core) or ("_" in core) or (alpha_ratio < 0.55)
                    # A real header always has at least one substantive word.
                    # Guards against stray fragments made ONLY of short
                    # conjunctions/prepositions (e.g. a line-bucketing
                    # artifact "OR AND" produced when a form prints "...CODE)
                    # OR HCPCS CODE) AND SUPPORTING..." across two words that
                    # render on a slightly different baseline than the rest of
                    # their line) — these pass every other all-caps check but
                    # carry no real header meaning.
                    _STOPWORDS = {
                        "OR", "AND", "OF", "THE", "TO", "A", "AN", "FOR",
                        "WITH", "IN", "ON", "AT", "BY", "IS", "IF", "AS",
                    }
                    core_tokens = re.findall(r"[A-Za-z]+", core)
                    has_real_word = any(
                        len(tok) >= 3 and tok.upper() not in _STOPWORDS
                        for tok in core_tokens
                    )
                    is_allcaps_band = (
                        5 <= len(core) <= 60
                        and len(letters_only) >= 4
                        and core == core.upper()
                        and any(c.isalpha() for c in core)
                        and not is_junky_header
                        and has_real_word
                    )
                    return {"cleaned": cleaned, "core": core, "sec": is_section_match, "cap": is_allcaps_band}

                page_sections: dict[int, list[tuple]] = {}
                for pnum, words in page_words.items():
                    page_w = page_widths.get(pnum, 612.0)
                    # Cluster words into lines by rounding top to nearest 4px
                    lines: dict[int, list[dict]] = defaultdict(list)
                    for w in words:
                        bucket = round(w["top"] / 4) * 4
                        lines[bucket].append(w)

                    anchors: list[tuple] = []  # (top, text, x0, x1)
                    for bucket in sorted(lines):
                        line_words = sorted(lines[bucket], key=lambda w: w["x0"])

                        # Split the row into runs on a big horizontal gap, then
                        # check for TWO+ side-by-side ENUMERATED section headers
                        # (e.g. "III. PROVIDER REQUESTING …" next to "IV.
                        # PRESCRIBING/PERFORMING …"). Merging these into one
                        # over-long line matches nothing, so each column's
                        # fields wrongly inherit the previous section. Only
                        # split when >=2 runs each independently look like an
                        # enumerated header — a single header with a far-right
                        # sub-label ("II. SERVICE INFORMATION   FOR PLAN USE
                        # ONLY") stays one full-width line as before.
                        runs: list[list[dict]] = []
                        for w in line_words:
                            if runs and (w["x0"] - runs[-1][-1]["x1"]) <= SECTION_RUN_GAP:
                                runs[-1].append(w)
                            else:
                                runs.append([w])
                        run_infos = []
                        for run in runs:
                            rtext = " ".join(w["text"] for w in run)
                            rinfo = _classify_header(rtext)
                            renum = bool(ENUM_RE.match(rtext))
                            run_infos.append((run, rinfo, renum))
                        enumerated_hdrs = [
                            ri for ri in run_infos
                            if ri[1] and (ri[1]["sec"] or ri[1]["cap"]) and ri[2]
                        ]
                        if len(enumerated_hdrs) >= 2:
                            bounds = [0.0]
                            for i in range(len(runs) - 1):
                                bounds.append((runs[i][-1]["x1"] + runs[i + 1][0]["x0"]) / 2.0)
                            bounds.append(page_w)
                            for idx, (run, rinfo, renum) in enumerate(run_infos):
                                if rinfo and (rinfo["sec"] or rinfo["cap"]) and renum:
                                    text = rinfo["cleaned"] if rinfo["sec"] else rinfo["core"]
                                    anchors.append((run[0]["top"], text, bounds[idx], bounds[idx + 1]))
                            continue

                        # Bold, colon-terminated headers (e.g. "Patient
                        # Information:" | "Prescribing Provider Information:").
                        # Some forms mark section headers by font WEIGHT, not
                        # ALL-CAPS / "Section N" / a shaded bar. Accept a row
                        # ONLY when it is (a) predominantly bold, (b) FIELD-FREE
                        # (a header row carries no input widget on its baseline
                        # — this is what separates the standalone header
                        # "Patient Information:" from the bold field prompt
                        # "Patient Name:" that has a text box to its right), and
                        # (c) each run reads like a heading ending in a colon.
                        # Side-by-side runs are scoped to their own column so
                        # left/right sections don't bleed (see _nearest_section).
                        if bucket in page_bold_buckets.get(pnum, set()):
                            line_y0 = min(w["top"] for w in line_words)
                            line_y1 = max(w.get("bottom", w["top"]) for w in line_words)
                            row_has_widget = any(
                                max(ff["y"], line_y0) < min(ff.get("y_bottom", ff["y"]), line_y1)
                                for ff in page_fields[pnum]
                            )
                            if not row_has_widget:
                                bold_runs = []
                                for run in runs:
                                    rtext = " ".join(w["text"] for w in run).strip()
                                    if not rtext.rstrip().endswith(":"):
                                        continue
                                    rcore = re.sub(r"\s*\([^)]*\)\s*$", "", rtext.strip(" :")).strip()
                                    toks = rcore.split()
                                    if not toks:
                                        continue
                                    first_alpha = next((ch for ch in rcore if ch.isalpha()), "")
                                    cap_words = sum(1 for t in toks if t[:1].isupper())
                                    title_ratio = cap_words / len(toks)
                                    letters = re.sub(r"[^A-Za-z]", "", rcore)
                                    heading_like = (
                                        first_alpha.isupper()
                                        and 3 <= len(rcore) <= 60
                                        and len(letters) >= 4
                                        and (rcore == rcore.upper() or title_ratio >= 0.6)
                                        and "#" not in rcore and "_" not in rcore
                                    )
                                    if heading_like:
                                        bold_runs.append((run, rcore))
                                if len(bold_runs) >= 2:
                                    sruns = [r for r, _ in bold_runs]
                                    hbounds = [0.0]
                                    for j in range(len(sruns) - 1):
                                        gap_lo = sruns[j][-1]["x1"]
                                        gap_hi = sruns[j + 1][0]["x0"]
                                        # Snap the column boundary to the actual
                                        # vertical divider ruling in the gap
                                        # (tallest one) rather than the text
                                        # midpoint — a left header can be much
                                        # shorter than its column, so the
                                        # midpoint would misassign fields sitting
                                        # between the header text and the divider.
                                        best_vx = None
                                        best_len = 0.0
                                        for vx, vspans in page_v_rulings.get(pnum, []):
                                            if not (gap_lo <= vx <= gap_hi):
                                                continue
                                            span_len = sum(hi - lo for lo, hi in vspans)
                                            if span_len > best_len:
                                                best_len = span_len
                                                best_vx = vx
                                        if best_vx is not None and best_len >= MULTIROW_FLOOR:
                                            hbounds.append(best_vx)
                                        else:
                                            hbounds.append((gap_lo + gap_hi) / 2.0)
                                    hbounds.append(page_w)
                                    for j, (run, rcore) in enumerate(bold_runs):
                                        anchors.append((run[0]["top"], rcore, hbounds[j], hbounds[j + 1]))
                                    continue
                                if len(bold_runs) == 1:
                                    run, rcore = bold_runs[0]
                                    anchors.append((run[0]["top"], rcore, 0.0, page_w))
                                    continue

                        # Single header (or a header + far-right sub-label):
                        # keep the whole-line, full-width behavior.
                        line_text = " ".join(w["text"] for w in line_words)
                        info = _classify_header(line_text)
                        if not info:
                            continue
                        top_val = line_words[0]["top"]
                        if info["sec"]:
                            anchors.append((top_val, info["cleaned"], 0.0, page_w))
                        elif info["cap"]:
                            # A row whose EVERY disjoint run sits immediately
                            # to the RIGHT of its OWN checkbox on this same
                            # row (e.g. a lone "☐ HEALTHWORX", or "☐ URGENT
                            # ☐ CAREADVANTAGE" split into two runs) is a row
                            # of checkbox OPTION LABEL(s), not a real section
                            # banner — even though it lands in one 4px line
                            # bucket and, joined, happens to read as an
                            # innocuous ALL-CAPS phrase. Registering it as a
                            # page-wide section anchor would otherwise block
                            # header/group matching for every OTHER checkbox
                            # on the page below this row (their section no
                            # longer matches this row's, since it has none) —
                            # and, since it's stored full-width [0, page_w],
                            # would wrongly prefix unrelated far-below field
                            # labels with this checkbox's own option text
                            # (e.g. "HEALTHWORX - Fax").
                            runs_after_checkbox = sum(
                                1 for run in runs
                                if any(
                                    "/Btn" in ff.get("type", "")
                                    and max(ff["y"], run[0]["top"]) < min(
                                        ff.get("y_bottom", ff["y"]),
                                        max(w.get("bottom", w["top"]) for w in run),
                                    )
                                    and 0 <= run[0]["x0"] - ff["x1"] <= SECTION_RUN_GAP
                                    for ff in page_fields[pnum]
                                )
                            )
                            if runs_after_checkbox >= 1 and runs_after_checkbox == len(runs):
                                continue
                            # A horizontally CENTERED all-caps line in the
                            # page's TITLE BAND (top ~18%) is a document title
                            # or confidentiality/disclaimer banner (e.g.
                            # "CONTAINS CONFIDENTIAL PATIENT INFORMATION"), not
                            # a section header — registering it would make it
                            # the fallback section (and boxed-table name) for
                            # every field below. The top-band scope is what
                            # keeps a legitimately centered *body* section
                            # header (e.g. a centered "MEMBER INFORMATION"
                            # banner further down the page) from being
                            # suppressed too.
                            ph = page_heights.get(pnum, 792.0)
                            left_m = line_words[0]["x0"]
                            right_m = page_w - line_words[-1]["x1"]
                            is_centered = (
                                left_m > 0.15 * page_w
                                and right_m > 0.15 * page_w
                                and abs(left_m - right_m) <= 0.12 * page_w
                            )
                            if is_centered and top_val < 0.18 * ph:
                                continue
                            anchors.append((top_val, info["core"], 0.0, page_w))

                    # Shaded-bar anchors: many forms (e.g. state PA forms) use
                    # a colored banner rect instead of ALL-CAPS or "Section N"
                    # text for headers (Title Case text sitting on a filled
                    # bar). Detect these directly from the page's vector
                    # rects, scoped to the bar's own x-range so side-by-side
                    # bars (e.g. two provider-info columns) don't bleed into
                    # each other — see _nearest_section.
                    # A table's own header ROW cell is often shaded the same
                    # gray as a real section banner (e.g. the IBX form's
                    # "Name of Assessment" header cell). Its width can clear
                    # BAR_MIN_WIDTH, so without this guard it would register
                    # as a bogus page section — stealing the section (and the
                    # table name) away from the real "SECTION D" banner above
                    # the grid. Skip any shaded rect whose center sits inside
                    # a detected table region.
                    _bar_tbl_regions = page_table_regions.get(pnum)
                    if _bar_tbl_regions is None:
                        _bar_tbl_regions = _detect_table_regions(pnum)
                        page_table_regions[pnum] = _bar_tbl_regions
                    seen_bar_keys: set = set()
                    for r in page_rects.get(pnum, []):
                        h = r["bottom"] - r["top"]
                        w_span = r["x1"] - r["x0"]
                        if not (BAR_MIN_HEIGHT <= h <= BAR_MAX_HEIGHT and w_span >= BAR_MIN_WIDTH):
                            continue
                        if not _is_bar_fill(r.get("non_stroking_color")):
                            continue
                        _bcx = (r["x0"] + r["x1"]) / 2.0
                        _bcy = (r["top"] + r["bottom"]) / 2.0
                        if any(
                            reg["x0"] - CELL_TOL <= _bcx <= reg["x1"] + CELL_TOL
                            and reg["top"] - CELL_TOL <= _bcy <= reg["bottom"] + CELL_TOL
                            for reg in _bar_tbl_regions
                        ):
                            continue
                        # De-dupe near-identical overlapping rects (border +
                        # fill often drawn as two stacked rects for one bar).
                        bkey = (round(r["top"] / 5), round(r["x0"] / 10), round(r["x1"] / 10))
                        if bkey in seen_bar_keys:
                            continue
                        seen_bar_keys.add(bkey)
                        bar_words = [
                            bw for bw in words
                            if r["top"] - 2 <= (bw["top"] + bw.get("bottom", bw["top"])) / 2.0 <= r["bottom"] + 2
                            and bw["x0"] >= r["x0"] - 3 and bw["x1"] <= r["x1"] + 3
                        ]
                        if not bar_words:
                            continue
                        bar_words.sort(key=lambda bw: bw["x0"])
                        # Some forms reuse the same background shading for
                        # single-field label cells (e.g. "*RequestedDME:",
                        # "*DMEPurchasePrice:$") as for real section banners.
                        # A genuine header is a standalone title, not a
                        # prompt — reject text that (structurally, before
                        # cleanup) ends in ":" or ":$" etc.
                        raw_bar_text = " ".join(bw["text"] for bw in bar_words).strip()
                        if raw_bar_text.rstrip("$").rstrip().endswith(":"):
                            continue
                        bar_text = _clean_label(raw_bar_text) or ""
                        bar_text = re.sub(r"\s*\([^)]*\)\s*$", "", bar_text).strip(" :)")
                        # A colored rect is not always a section banner — forms
                        # also use shading for instructional callouts /
                        # disclaimers (e.g. a green box reading "... must be
                        # submitted with prior authorization request)"). A real
                        # banner is a heading: it starts capitalized and is
                        # mostly Title Case or ALL-CAPS. Reject sentence-like
                        # text (starts lowercase, or few capitalized words) so
                        # such callouts don't become false section anchors that
                        # then contaminate nearby field labels.
                        bar_tokens = bar_text.split()
                        first_alpha = next((c for c in bar_text if c.isalpha()), "")
                        cap_words = sum(1 for t in bar_tokens if t[:1].isupper())
                        title_ratio = cap_words / len(bar_tokens) if bar_tokens else 0.0
                        looks_heading = bool(bar_tokens) and first_alpha.isupper() and (
                            bar_text == bar_text.upper() or title_ratio >= 0.6
                        )
                        if bar_text and 3 <= len(bar_text) <= 80 and looks_heading:
                            anchors.append((r["top"], bar_text, r["x0"], r["x1"]))

                    # Fold in "Section N" headers recovered by font de-inter-
                    # leaving that the line-based passes above missed (their
                    # baseline was woven together with a field-label row). Add
                    # each as a full-width anchor, skipping EXACT duplicates the
                    # normal passes already found on the same 4px band.
                    _existing = {(round(a[0] / 4) * 4, a[1]) for a in anchors}
                    for _top, _txt in page_font_section_anchors.get(pnum, []):
                        _info = _classify_header(_txt)
                        if not (_info and _info["sec"]):
                            continue
                        if (round(_top / 4) * 4, _info["cleaned"]) in _existing:
                            continue
                        anchors.append((_top, _info["cleaned"], 0.0, page_w))

                    page_sections[pnum] = sorted(anchors, key=lambda t: t[0])

                def _nearest_section(pnum: int, f_top: float, f_x0: float = 0.0, f_x1: float = 0.0) -> str | None:
                    """Return the nearest section header above f_top.

                    Prefers an anchor whose x-range overlaps the field's own
                    column (needed for column-scoped bar anchors, e.g. two
                    side-by-side "Provider Information" banners); falls back
                    to the nearest anchor regardless of x (legacy behavior —
                    used by full-width SECTION_RE/ALL-CAPS anchors, which are
                    stored with x-range [0, page_width] and so always match
                    anyway).
                    """
                    anchors = page_sections.get(pnum, [])
                    f_center = (f_x0 + f_x1) / 2.0 if (f_x0 or f_x1) else f_x0
                    best = None
                    best_any = None
                    for (atop, atext, ax0, ax1) in anchors:
                        if atop > f_top:
                            break
                        best_any = atext
                        if ax0 - 2 <= f_center <= ax1 + 2:
                            best = atext
                    return best if best is not None else best_any

                # ── Part A2: detect column-header checkbox grids ────────────
                # Some forms lay out checkboxes in a MULTI-COLUMN grid under
                # headers ending in ":" (e.g. "Inpatient Care:", "Outpatient/
                # Office Care:", "Therapies:"), each governing several ROWS of
                # checkbox options below it. This differs from the same-row
                # qualifier ("Sex: Male", Part B below) and the above-row
                # group header ((a)/(b) checks below), which only look a few
                # px above a single checkbox's own row — a column header can
                # sit far above the lower rows it governs. Stored per page as
                # (top, text, col_x0, col_x1); consumed by the checkbox
                # group-header block further down via _nearest_column_group.
                COLGRID_CHAIN_GAP = 15   # px: max gap to join words into one header phrase
                COLGRID_BELOW_MIN = 8    # px: a governed checkbox must sit on a genuinely LOWER row, not
                                         # just glyph-baseline jitter on the header's own visual line (e.g.
                                         # "DOB:" and an adjacent-column "Male" checkbox 3px below are the
                                         # SAME row, not a header-governs-rows-below relationship)
                COLGRID_BELOW_MAX = 120  # px: how far below a header to look for a governed checkbox
                COLGRID_PAD       = 5    # px: column boundary padding
                COLGRID_ADJ_FIELD_GAP = 40  # px: a field this close to the RIGHT of a ":"-candidate
                                            # means it's that field's own printed label ("Health Plan:
                                            # [Health Plan textbox]"), not a column-grid header — a real
                                            # column header has no input box of its own; it only governs
                                            # rows of checkboxes BELOW it.
                COLGRID_SIBLING_EXEMPT_MAX = 34  # px: tighter cap on the checkbox-below sibling-row
                                                  # exemption (see below) — a genuine grid header's OWN
                                                  # first FEW governed options sit close underneath it
                                                  # (e.g. Vermont's "InpatientCare:" 2nd row option at
                                                  # ~31px), while an unrelated coincidental match from a
                                                  # DIFFERENT grid several rows down (e.g. "Health plan
                                                  # fax:" picking up an unrelated checklist's first row at
                                                  # ~37px) sits just past this cap.

                page_column_headers: dict[int, list[tuple]] = {}
                for pnum, words in page_words.items():
                    page_w = page_widths.get(pnum, 612.0)
                    lines: dict[int, list[dict]] = defaultdict(list)
                    for w in words:
                        bucket = round(w["top"] / 4) * 4
                        lines[bucket].append(w)

                    col_headers: list[tuple] = []  # (top, text, x0, x1)
                    for bucket in sorted(lines):
                        line_words = sorted(lines[bucket], key=lambda w: w["x0"])
                        row_top = line_words[0]["top"]
                        row_bottom = max(w.get("bottom", w["top"]) for w in line_words)
                        # Chain adjacent words into phrases, splitting the row
                        # into separate column-header candidates on a bigger gap.
                        runs: list[list[dict]] = []
                        for w in line_words:
                            if runs and (w["x0"] - runs[-1][-1]["x1"]) <= COLGRID_CHAIN_GAP:
                                runs[-1].append(w)
                            else:
                                runs.append([w])
                        # How many runs on this row read like a plausible
                        # header candidate AT ALL (colon-terminated with a
                        # sane length, or a standalone heading) — used below
                        # to tell a genuine multi-column grid (2+ SIBLING
                        # headers side by side, e.g. "InpatientCare:
                        # Outpatient/OfficeCare: Therapies:" or "REQUEST
                        # LINE OF BUSINESS") apart from a lone header whose
                        # row also happens to contain unrelated prose
                        # fragments chained separately by big gaps (e.g.
                        # "for patient with a pathologically proven  Chest
                        # x-ray results:  cancer (unintentional weight
                        # loss..." — 3 raw runs, but only ONE is an actual
                        # header candidate).
                        def _is_plausible_header_text(raw: str) -> bool:
                            if raw.endswith(":"):
                                t = (_clean_label(raw) or raw).rstrip(" :")
                                return bool(t) and 2 <= len(t) <= 40
                            return _is_headingish(_clean_label(raw) or raw)

                        sibling_header_count = sum(
                            1 for run in runs
                            if _is_plausible_header_text(" ".join(w["text"] for w in run).strip())
                        )

                        candidates: list[tuple] = []  # (x0, x1, text, is_colon)
                        for run in runs:
                            raw_text = " ".join(w["text"] for w in run).strip()
                            is_colon = raw_text.endswith(":")
                            if is_colon:
                                text = (_clean_label(raw_text) or raw_text).rstrip(" :")
                                if not (text and 2 <= len(text) <= 40):
                                    continue
                            else:
                                # No punctuation cue of its own — only accept
                                # when it reads like a standalone heading
                                # ("REQUEST", "LINE OF BUSINESS"), never
                                # arbitrary body text that happens to sit
                                # above/beside a checkbox (see _is_headingish).
                                text = _clean_label(raw_text) or raw_text
                                if not _is_headingish(text):
                                    continue
                            cand_x0, cand_x1 = run[0]["x0"], run[-1]["x1"]
                            # A header candidate must be text-only in its OWN
                            # x-span — a widget occupying that exact spot means
                            # this text is a field label, not a column header.
                            # Scoped to the candidate's x-range (not the whole
                            # row) so an unrelated tall widget elsewhere on the
                            # same row (e.g. a text box far to the right whose
                            # top edge grazes this row's bottom edge) can't
                            # disqualify the header.
                            if any(
                                max(ff["y"], row_top) < min(ff.get("y_bottom", ff["y"]), row_bottom)
                                and max(ff["x0"], cand_x0) < min(ff["x1"], cand_x1)
                                for ff in page_fields[pnum]
                            ):
                                continue
                            # Reject a candidate immediately followed (same
                            # row, small gap) by ANY field — that's a plain
                            # "Label: [box]" pair, not a header governing rows
                            # below it (see COLGRID_ADJ_FIELD_GAP above).
                            if any(
                                max(ff["y"], row_top) < min(ff.get("y_bottom", ff["y"]), row_bottom)
                                and 0 <= ff["x0"] - cand_x1 <= COLGRID_ADJ_FIELD_GAP
                                for ff in page_fields[pnum]
                            ):
                                continue
                            # Same idea, but for a TEXT field directly BELOW
                            # the header in its OWN narrow x-span (e.g.
                            # "DOB:" with its own date textbox right
                            # underneath) — that's a normal label-above-its-
                            # field pair, not a header governing an unrelated
                            # grid of options. Scoped to the header's OWN
                            # text span (not the wide column later assigned
                            # to it, which can extend far to the right and
                            # wrongly overlap completely unrelated checkboxes
                            # there).
                            #
                            # Colon-terminated ONLY: a colon-less heading has
                            # no "Label: [own field]" reading in the first
                            # place, and its FIRST governed checkbox is
                            # commonly printed close underneath (e.g.
                            # "REQUEST" with "Urgent" ~19px below it) — this
                            # gate would otherwise reject the header for the
                            # very checkbox it is meant to govern.
                            #
                            # Excludes CHECKBOX fields deliberately, but ONLY
                            # when this header has a SIBLING header candidate
                            # on the SAME text row (sibling_header_count >= 2)
                            # — a real multi-column grid always prints its
                            # column headers side by side on one row (e.g.
                            # "InpatientCare:  Outpatient/OfficeCare:
                            # Therapies:", or "REQUEST  LINE OF BUSINESS").
                            # A checkbox sitting directly below such a header
                            # is virtually always the header's
                            # FIRST governed option, not a dedicated answer
                            # box — and multi-column grids with long headers
                            # routinely have their first checkbox's LEFT edge
                            # land just inside the header text's own x-span
                            # purely because of the header's length, which
                            # would otherwise wrongly reject the header the
                            # checkbox is meant to be governed by (collapsing
                            # every column into the one surviving header).
                            #
                            # A LONE header occupying its ENTIRE row by itself
                            # (e.g. "Request is for: Orencia (abatacept):",
                            # "For Initiation requests (...):") is a
                            # DIFFERENT pattern — an introductory prompt for
                            # one immediately-following answer, not a grid
                            # column — and must still be rejected here so it
                            # can't be validated and then, via
                            # COLGRID_MAX_REACH's fallback distance, keep
                            # "governing" unrelated checkboxes hundreds of px
                            # further down the page.
                            #
                            # Also requires the field to NOT start meaningfully
                            # to the LEFT of the header's own x0 — a wide text
                            # box belonging to an unrelated, LATER prompt (e.g.
                            # a "*DateDiagnosed:" answer box spanning far to
                            # the left) can clip through an earlier header's
                            # narrow column purely by extending underneath it
                            # from well before its start, without actually
                            # being that header's own dedicated field. A
                            # governed checkbox sitting anywhere AT or AFTER
                            # the header's own x0 (e.g. right after its
                            # trailing ":") is still fair game.
                            #
                            # The sibling exemption itself is further capped
                            # to a TIGHT reach (COLGRID_SIBLING_EXEMPT_MAX) —
                            # two simple label prompts sharing one text row
                            # (e.g. "Health plan:" / "Health plan fax:", two
                            # unrelated fields, not grid columns) can still
                            # coincidentally sit just above an ENTIRELY
                            # separate, unrelated checkbox grid several rows
                            # below; a genuine multi-column header's FIRST
                            # governed option is always printed right
                            # underneath it, not merely "somewhere within the
                            # generous same-row-field gap allowance".
                            if is_colon and any(
                                COLGRID_BELOW_MIN <= (ff["y"] - row_top) <= COLGRID_ADJ_FIELD_GAP
                                and max(ff["x0"], cand_x0) < min(ff["x1"], cand_x1)
                                and ff["x0"] >= cand_x0 - COLGRID_PAD
                                and (
                                    "/Btn" not in ff.get("type", "")
                                    or sibling_header_count < 2
                                    or (ff["y"] - row_top) > COLGRID_SIBLING_EXEMPT_MAX
                                )
                                for ff in page_fields[pnum]
                            ):
                                continue
                            # A colon-less candidate has no punctuation cue of
                            # its own, so a checkbox sitting close on the SAME
                            # row to its immediate LEFT means this text is
                            # simply THAT checkbox's own printed option label
                            # (e.g. "☐ CAREADVANTAGE" — the checkbox is one
                            # entry in the vertical stack the real heading
                            # governs), not a standalone group heading —
                            # reject it. Real headers ("REQUEST", "LINE OF
                            # BUSINESS") sit on a row with NO checkbox at all.
                            if not is_colon and any(
                                "/Btn" in ff.get("type", "")
                                and max(ff["y"], row_top) < min(ff.get("y_bottom", ff["y"]), row_bottom)
                                and 0 <= cand_x0 - ff["x1"] <= COLGRID_ADJ_FIELD_GAP
                                for ff in page_fields[pnum]
                            ):
                                continue
                            candidates.append((cand_x0, cand_x1, text, is_colon))
                        # Column boundary between header i and i+1 defaults
                        # to the SHARED midpoint of the gap between them —
                        # using each header's own text edges independently
                        # (e.g. "right edge of header i" as header i+1's
                        # left bound) makes adjacent columns overlap by the
                        # width of the gap, since header i's own range
                        # already extends up to header i+1's start.
                        #
                        # But prefer a REAL ruled vertical divider in that
                        # gap when one exists: a header's own printed-text
                        # width is not a reliable proxy for where the actual
                        # cell wall sits (e.g. "InpatientCare:" ends well
                        # short of its column's true right edge, so the
                        # midpoint falls short of the wall and a checkbox
                        # placed near the wall — still legitimately in THIS
                        # column — gets misassigned to the next one).
                        boundaries = [0.0]
                        for i in range(len(candidates) - 1):
                            gap_lo, gap_hi = candidates[i][1], candidates[i + 1][0]
                            mid = (gap_lo + gap_hi) / 2.0
                            wall = None
                            for vx, spans in page_v_rulings.get(pnum, []):
                                if not (gap_lo < vx < gap_hi):
                                    continue
                                if any((hi - lo) >= MULTIROW_FLOOR for lo, hi in spans):
                                    if wall is None or abs(vx - mid) < abs(wall - mid):
                                        wall = vx
                            boundaries.append(wall if wall is not None else mid)
                        boundaries.append(page_w)
                        COLGRID_HEADINGISH_PAD = 30       # px: how far a colon-less heading's OWN
                                                           # governed checkboxes may sit from its text
                        COLGRID_HEADINGISH_BELOW_MAX = 70 # px: tighter than COLGRID_BELOW_MAX — a
                                                           # colon-less heading has no ":" cue of its
                                                           # own, so it is trusted only when its
                                                           # checkboxes sit CLOSE underneath (e.g.
                                                           # "REQUEST" -> "Urgent" 19px below); a distant
                                                           # match (~90-110px, e.g. an unrelated Yes/No
                                                           # pair under a page title) is far more likely
                                                           # coincidental x-overlap than real governance.
                        for i, (cx0, cx1, text, is_colon) in enumerate(candidates):
                            col_x0 = boundaries[i]
                            col_x1 = boundaries[i + 1]
                            if is_colon:
                                # Gate: only a real column-grid header if a real
                                # checkbox exists BELOW it (not just beside it, on
                                # its own row) within its column span — this
                                # excludes single-row prompts like "Gender:"
                                # (options sit to the RIGHT on the same row, which
                                # is already excluded by the text-only-row check
                                # above, or there's simply nothing below it at all).
                                has_checkbox_below = any(
                                    "/Btn" in f.get("type", "")
                                    and COLGRID_BELOW_MIN <= (f["y"] - row_top) <= COLGRID_BELOW_MAX
                                    and col_x0 - COLGRID_PAD <= (f["x0"] + f["x1"]) / 2.0 <= col_x1 + COLGRID_PAD
                                    for f in page_fields[pnum]
                                )
                                if has_checkbox_below:
                                    col_headers.append((row_top, text, col_x0, col_x1))
                            else:
                                # A colon-less heading has no punctuation cue,
                                # so it is held to a STRICTER geometric bar:
                                # its governed checkboxes must sit tightly
                                # under its OWN printed text (± a small pad),
                                # not merely anywhere within the wide
                                # neighbor-midpoint span (which would also
                                # sweep in an unrelated checkbox group that
                                # happens to share the same row, e.g. a
                                # "hospitalized Yes/No" pair far to the left
                                # of "REQUEST"/"LINE OF BUSINESS"). Require
                                # >= 2 governed checkboxes — a single stray
                                # match is more likely coincidence than a
                                # real column-grid heading.
                                search_x0 = max(col_x0, cx0 - COLGRID_HEADINGISH_PAD)
                                search_x1 = min(col_x1, cx1 + COLGRID_HEADINGISH_PAD)
                                governed = [
                                    f for f in page_fields[pnum]
                                    if "/Btn" in f.get("type", "")
                                    and COLGRID_BELOW_MIN <= (f["y"] - row_top) <= COLGRID_HEADINGISH_BELOW_MAX
                                    and search_x0 <= (f["x0"] + f["x1"]) / 2.0 <= search_x1
                                ]
                                if len(governed) >= 2:
                                    tight_x0 = min([cx0] + [f["x0"] for f in governed]) - COLGRID_PAD
                                    tight_x1 = max([cx1] + [f["x1"] for f in governed]) + COLGRID_PAD
                                    col_headers.append((row_top, text, tight_x0, tight_x1))

                    page_column_headers[pnum] = sorted(col_headers, key=lambda t: t[0])

                COLGRID_MAX_REACH = 250  # px: fallback cap when no ruling/section boundary bounds the header's reach

                def _nearest_column_group(pnum: int, f_top: float, f_x0: float, f_x1: float) -> str | None:
                    """Return the nearest column-grid header above a checkbox
                    whose column x-range contains the checkbox's x-center.

                    Matching purely on x-range with no limit on HOW FAR above
                    a header can sit let a header validated for one nearby
                    checkbox row (e.g. "Issuer Name" or "DOB" near the top of
                    the form) get reused, hundreds of px later, by an
                    unrelated checkbox far down the page that happens to
                    share the same column x-range (e.g. TDI's place-of-
                    service row). Bound the reach: a header only governs
                    checkboxes in its OWN section, and only within a sane
                    fallback distance when no section info applies.
                    """
                    f_center = (f_x0 + f_x1) / 2.0
                    field_section = _nearest_section(pnum, f_top, f_x0, f_x1)
                    best = None
                    for (htop, htext, hx0, hx1) in page_column_headers.get(pnum, []):
                        if htop > f_top:
                            break
                        if not (hx0 - COLGRID_PAD <= f_center <= hx1 + COLGRID_PAD):
                            continue
                        header_section = _nearest_section(pnum, htop, hx0, hx1)
                        if header_section != field_section:
                            continue
                        if f_top - htop > COLGRID_MAX_REACH:
                            continue
                        best = htext
                    return best

                CELL_HEADER_MAX_LEN = 60  # px-of-chars: reject implausibly long "header" text

                def _cell_header_group(pnum: int, f_top: float, f_bottom: float, f_x0: float, f_x1: float) -> str | None:
                    """Return a checkbox's group from its enclosing printed
                    table cell, for grids whose headers DON'T end in ":" (see
                    _nearest_column_group above, which handles the ":" case).
                    A cell qualifies as a checkbox group only when it fully
                    encloses the field (all 4 rulings found) and contains 2+
                    checkboxes — the header is then the cell's topmost
                    text-only line(s), read before the first line that has a
                    checkbox on it."""
                    cx = (f_x0 + f_x1) / 2.0
                    cell = _cell_bounds(pnum, cx, f_top, f_bottom)
                    cx0, cx1, ctop, cbottom = cell["x0"], cell["x1"], cell["top"], cell["bottom"]
                    if cx0 is None or cx1 is None or ctop is None or cbottom is None:
                        return None

                    cb_in_cell = [
                        f for f in page_fields.get(pnum, [])
                        if "/Btn" in f.get("type", "")
                        and cx0 - CELL_TOL <= (f["x0"] + f["x1"]) / 2.0 <= cx1 + CELL_TOL
                        and ctop - CELL_TOL <= (f["y"] + f.get("y_bottom", f["y"])) / 2.0 <= cbottom + CELL_TOL
                    ]
                    if len(cb_in_cell) < 2:
                        return None

                    cell_words = [
                        w for w in page_words.get(pnum, [])
                        if cx0 - CELL_TOL <= (w["x0"] + w["x1"]) / 2.0 <= cx1 + CELL_TOL
                        and ctop - CELL_TOL <= w["top"] <= cbottom + CELL_TOL
                    ]
                    if not cell_words:
                        return None

                    lines: dict[int, list[dict]] = defaultdict(list)
                    for w in cell_words:
                        bucket = round(w["top"] / 4) * 4
                        lines[bucket].append(w)

                    header_words: list[dict] = []
                    for bucket in sorted(lines):
                        line_ws = lines[bucket]
                        row_top = min(w["top"] for w in line_ws)
                        row_bottom = max(w.get("bottom", w["top"]) for w in line_ws)
                        has_checkbox = any(
                            _row_overlap(row_top, row_bottom, f["y"], f.get("y_bottom", f["y"]))
                            for f in cb_in_cell
                        )
                        if has_checkbox:
                            break
                        header_words.extend(sorted(line_ws, key=lambda w: w["x0"]))

                    if not header_words:
                        return None
                    text = _clean_label(" ".join(w["text"] for w in header_words))
                    if not text or len(text) > CELL_HEADER_MAX_LEN:
                        return None
                    return text

                # ── Per-field label loop ─────────────────────────────────────
                for field in fields_info:
                    pnum  = field["page"]
                    words = page_words.get(pnum, [])
                    f_top = field["y"]
                    f_bottom = field.get("y_bottom", f_top)
                    f_x0  = field["x0"]
                    f_x1  = field["x1"]

                    # A field is a checkbox if the AcroForm says so (/Btn) or,
                    # as a last-resort fallback for widgets near the left
                    # margin whose label sits to their right, when x0 is below
                    # the threshold. (The positional fallback is intentionally
                    # broad — many left-margin /Tx prompts extract better via
                    # the checkbox path — EXCEPT it is overridden just below
                    # for composite split fields.)
                    _ftype = field.get("type", "")
                    is_checkbox = ("/Btn" in _ftype) or f_x0 < CHECKBOX_X_THRESHOLD

                    # Printed cell enclosing this field, if the form is a
                    # bordered grid here — used below to stop label/qualifier
                    # scans from bleeding across a ruled cell wall into a
                    # neighboring column/row (fixed pixel margins alone can't
                    # know where the columns actually are).
                    field_cell = _cell_bounds(pnum, (f_x0 + f_x1) / 2.0, f_top, f_bottom)
                    cell_x0, cell_x1, cell_top = field_cell["x0"], field_cell["x1"], field_cell["top"]
                    cell_bottom = field_cell["bottom"]
                    # A genuine table (rows AND columns) requires the field to
                    # sit inside a detected ruling LATTICE (see
                    # _detect_table_regions) — a single bordered label box
                    # (e.g. "Issuer Name", "DOB") has 4 rulings around it too
                    # but is NOT part of a multi-row/column grid, so it must
                    # not qualify.
                    table_idx, table_region = _table_region_for(pnum, (f_x0 + f_x1) / 2.0, (f_top + f_bottom) / 2.0)
                    is_grid_cell = table_region is not None

                    # Compare by object IDENTITY (not name) to exclude only
                    # THIS widget — radio-group siblings (e.g. the "Male" and
                    # "Female" option widgets of one "Sex" field) share the
                    # SAME field name, so a name-based exclusion would wrongly
                    # drop the sibling from "same_row" too, and with it the
                    # boundary that stops label scans from bleeding into the
                    # neighboring option's cell (e.g. "Male" capturing
                    # "Female" as well).
                    same_row = [
                        ff for ff in page_fields[pnum]
                        if ff is not field
                        and _fields_same_row(ff, field)
                    ]

                    # ── Row lead-in header (left of leftmost same-row box) ──
                    # Some forms print a descriptive header to the LEFT of an
                    # entire run of same-row checkboxes, with each checkbox's
                    # own option text to ITS right (e.g. "Review Type
                    # Requested [ ] Standard [ ] Urgent", "This medication
                    # will be administered by [ ] Individual/Caregiver
                    # [ ] Pharmacist [ ] Other Healthcare Provider") — neither
                    # a per-checkbox "Label ☐" nor a short "Sex:" qualifier.
                    # Detected once per row-group (keyed identically to
                    # synthetic_row_groups) and shared verbatim by every
                    # checkbox in it. Used below to (b) stop the header being
                    # mistaken for the leftmost box's OWN option label and
                    # (c) stop it being re-merged as a Part-B qualifier, so it
                    # lives only in `group`, never in the option label.
                    row_lead_group: str | None = None
                    if "/Btn" in field.get("type", ""):
                        row_cbs = [field] + [ff for ff in same_row if "/Btn" in ff.get("type", "")]
                        if len(row_cbs) >= 2:
                            # Page-scoped: many multi-page forms reuse generic
                            # positional field names ("10", "20"...) per page
                            # for entirely unrelated widgets — without pnum,
                            # a header resolved for one page's row leaks into
                            # a same-named-but-different row on another page.
                            row_key = (pnum, tuple(sorted({self._widget_key(ff) for ff in row_cbs})))
                            if row_key in row_leadin_groups:
                                row_lead_group = row_leadin_groups[row_key]
                            else:
                                _L = min(row_cbs, key=lambda ff: ff["x0"])
                                _l_top, _l_bottom = _L["y"], _L.get("y_bottom", _L["y"])
                                _l_cell = _cell_bounds(pnum, (_L["x0"] + _L["x1"]) / 2.0, _l_top, _l_bottom)
                                # Bounded left by L's own enclosing cell wall
                                # so the scan never crosses a ruled cell wall
                                # into an unrelated column; without a
                                # detected cell, fall back to a generous
                                # fixed window.
                                _lead_left_bound = (
                                    _l_cell["x0"] if _l_cell.get("x0") is not None
                                    else max(0, _L["x0"] - 250)
                                )
                                _lead_words = [
                                    w for w in words
                                    if _word_on_row(w, _l_top, _l_bottom)
                                    and w["x1"] <= _L["x0"] - 2
                                    and w["x0"] >= _lead_left_bound
                                    and not _is_glyph_word(w["text"])
                                ]
                                _lead_text: str | None = None
                                if _lead_words:
                                    _lead_words.sort(key=lambda w: w["x0"])
                                    _phrase = _clean_label(" ".join(w["text"] for w in _lead_words))
                                    # Header-ish: multi-word or long enough to
                                    # be a real prompt, not a bare stray word
                                    # (keeps short fragments out).
                                    # Reject sentence fragments (a genuine
                                    # header/prompt never starts with a
                                    # footnote marker like "(*" or ends with
                                    # a full stop — those signal a clipped
                                    # piece of a longer disclaimer sentence
                                    # that happens to end its line just
                                    # above/left of the checkbox run).
                                    if (
                                        _phrase
                                        and (len(_phrase.split()) >= 2 or len(_phrase) >= 12)
                                        and "_" not in _phrase
                                        and _phrase[0].isalnum()
                                        and not _phrase.rstrip().endswith(".")
                                    ):
                                        _lead_text = _phrase
                                row_leadin_groups[row_key] = _lead_text
                                row_lead_group = _lead_text

                    # ── Composite split-field detection ─────────────────────
                    # A single value split across adjacent boxes on one row
                    # with only SEPARATOR glyphs printed between them — a date
                    # "[MM] / [DD] / [YYYY]", an SSN, a phone "( ) [ ] - [ ]".
                    # The boxes share ONE label printed ABOVE the whole run
                    # (e.g. "Date of Birth (mm/dd/yyyy)"), not one per box.
                    # Detect the contiguous cluster around this field (adjacent
                    # TEXT widgets whose gap holds only separator words and is
                    # narrow) and remember its merged x-span so the ABOVE scan
                    # reads the full shared header — identically for every
                    # member, so they don't collide on a bare "/" and get
                    # section-prefixed into "CASE INFORMATION - /".
                    #
                    # Gated on the ACTUAL /Tx type (not the is_checkbox
                    # heuristic): a split date's leftmost boxes hug the left
                    # margin (x0 < CHECKBOX_X_THRESHOLD) and would otherwise be
                    # mis-driven through the checkbox path. When a composite is
                    # confirmed we force is_checkbox=False for THIS field only
                    # (before the bound/label logic below), so left-margin
                    # split-date boxes are treated as the text fields they are
                    # — without disturbing the broad left-margin fallback that
                    # other (non-composite) left-margin prompts still rely on.
                    comp_x0, comp_x1 = f_x0, f_x1
                    is_composite = False
                    if "/Tx" in _ftype:
                        MAX_COMPOSITE_GAP = 45  # px between adjacent split boxes
                        _text_row = sorted(
                            [ff for ff in same_row if "/Btn" not in ff.get("type", "")]
                            + [field],
                            key=lambda ff: ff["x0"],
                        )
                        try:
                            _idx = next(i for i, ff in enumerate(_text_row) if ff is field)
                        except StopIteration:
                            _idx = None
                        if _idx is not None and len(_text_row) >= 2:
                            def _gap_only_separators(a: dict, b: dict) -> bool:
                                if b["x0"] - a["x1"] > MAX_COMPOSITE_GAP or b["x0"] < a["x1"] - 2:
                                    return False
                                between = [
                                    w for w in words
                                    if _word_on_row(w, f_top, f_bottom)
                                    and w["x0"] >= a["x1"] - 2
                                    and w["x1"] <= b["x0"] + 2
                                ]
                                # Require a DATE SLASH separator ("/") printed
                                # between the two boxes and NOTHING else. This
                                # deliberately targets only slash-dates
                                # "[MM] / [DD] / [YYYY]" — the case whose boxes
                                # share one header above (e.g. "Date of Birth
                                # (mm/dd/yyyy)"). An EMPTY gap means two ordinary
                                # adjacent fields ("Name [ ] Phone [ ]"); a gap
                                # holding parens/dashes is a phone "( ) -" or
                                # SSN run whose parts belong to DIFFERENT labeled
                                # fields (NPI / Phone / Fax), so they must NOT be
                                # welded into one composite.
                                if not between:
                                    return False
                                if not all(_is_separator_word(w["text"]) for w in between):
                                    return False
                                joined = "".join(_strip_glyphs(w["text"]) for w in between)
                                return "/" in joined and "(" not in joined and ")" not in joined

                            _lo = _hi = _idx
                            while _lo > 0 and _gap_only_separators(_text_row[_lo - 1], _text_row[_lo]):
                                _lo -= 1
                            while _hi < len(_text_row) - 1 and _gap_only_separators(_text_row[_hi], _text_row[_hi + 1]):
                                _hi += 1
                            if _hi > _lo:  # at least 2 boxes in the cluster
                                is_composite = True
                                _comp_members = [_text_row[i] for i in range(_lo, _hi + 1)]
                                comp_x0 = min(m["x0"] for m in _comp_members)
                                comp_x1 = max(m["x1"] for m in _comp_members)

                    # A confirmed composite split-field is always a text field,
                    # even if it hugs the left margin — override the positional
                    # checkbox fallback for it.
                    composite_label: str | None = None
                    if is_composite:
                        is_checkbox = False
                        # Resolve the run's ONE shared label once, cached so
                        # every box reads the same text. Prefer a printed prompt
                        # to the LEFT of the leftmost box (e.g. "Start of
                        # treatment: Start date ___/___/___"); only fall back to
                        # the header directly ABOVE the whole run (e.g. "Date of
                        # Birth (mm/dd/yyyy)" over left-margin boxes) when there
                        # is no such left prompt.
                        # Page-scoped for the same reason as row_key above —
                        # generic positional field names repeat per page.
                        _comp_key = (pnum, tuple(sorted({self._widget_key(m) for m in _comp_members})))
                        if _comp_key in composite_labels:
                            composite_label = composite_labels[_comp_key]
                        else:
                            _lm = min(_comp_members, key=lambda m: m["x0"])
                            _lm_top, _lm_bottom = _lm["y"], _lm.get("y_bottom", _lm["y"])
                            _lm_cell = _cell_bounds(pnum, (_lm["x0"] + _lm["x1"]) / 2.0, _lm_top, _lm_bottom)
                            _comp_left_bound = (
                                _lm_cell["x0"] if _lm_cell.get("x0") is not None
                                else max(0, _lm["x0"] - 220)
                            )
                            _cl_words = [
                                w for w in words
                                if _word_on_row(w, _lm_top, _lm_bottom)
                                and w["x1"] <= _lm["x0"] - 2
                                and w["x0"] >= _comp_left_bound
                                and not _is_glyph_word(w["text"])
                                and not _is_separator_word(w["text"])
                            ]
                            if _cl_words:
                                _cl_words.sort(key=lambda w: w["x0"])
                                # Keep only the contiguous run of words that
                                # abuts the box (gap ≤ 45px), and require its
                                # rightmost word to sit close to the box's left
                                # edge (≤ 60px). This grabs a genuine prompt
                                # ("Start date ___", "From ___") without
                                # reaching across a wide gap into an unrelated
                                # neighbour's text ("Male Female" left of a
                                # date box).
                                if _lm["x0"] - _cl_words[-1]["x1"] <= 60:
                                    _chain = [_cl_words[-1]]
                                    for w in reversed(_cl_words[:-1]):
                                        if _chain[-1]["x0"] - w["x1"] <= 45:
                                            _chain.append(w)
                                        else:
                                            break
                                    _chain.reverse()
                                    composite_label = _clean_label(
                                        " ".join(w["text"] for w in _chain[-MAX_LABEL_WORDS:])
                                    ) or None
                            if not composite_label:
                                # Above the merged run, nearest row.
                                _ca = [
                                    w for w in words
                                    if 4 <= (_lm_top - w["top"]) <= 50
                                    and w["x1"] > comp_x0 - 2
                                    and w["x0"] < comp_x1 + 2
                                ]
                                if _ca:
                                    _cn = max(w["top"] for w in _ca)
                                    _crow = sorted(
                                        [w for w in _ca if abs(w["top"] - _cn) <= 6],
                                        key=lambda w: w["x0"],
                                    )
                                    composite_label = _clean_label(
                                        " ".join(w["text"] for w in _crow)
                                    ) or None
                            composite_labels[_comp_key] = composite_label

                    # ── Left colon-prompt override for left-margin text
                    # fields ──────────────────────────────────────────────
                    # Some forms print a plain "Phone: ____", "Member ID#:
                    # ____" prompt immediately to a field's LEFT with the
                    # input area just an underline (no visible box) — for a
                    # /Tx field sitting near the left margin (x0 below
                    # CHECKBOX_X_THRESHOLD, otherwise mis-driven onto the
                    # checkbox path by the positional fallback below) this
                    # must be read directly from the ":"-terminated prompt
                    # on ITS OWN left. Left uncorrected, the checkbox path's
                    # right-scan/Part-B-qualifier logic instead bleeds into
                    # the NEXT field's own left prompt on the same row (e.g.
                    # "Phone: ____ Fax: ____" → "Phone: Fax: ____" merging
                    # two different fields' labels into one). Excludes
                    # composites, which already have their own label.
                    left_colon_label: str | None = None
                    if not is_composite and "/Tx" in _ftype and f_x0 < CHECKBOX_X_THRESHOLD:
                        LEFT_COLON_MAX_GAP = 15   # px: widget<-prompt gap threshold
                        LEFT_COLON_CHAIN_GAP = 12  # px: max gap between chained prompt words
                        _lc_left_bound = cell_x0 if cell_x0 is not None else max(0, f_x0 - 200)
                        _lc_words = sorted(
                            [
                                w for w in words
                                if _word_on_row(w, f_top, f_bottom)
                                and w["x1"] <= f_x0 + 2
                                and w["x0"] >= _lc_left_bound
                                and not _is_glyph_word(w["text"])
                            ],
                            key=lambda w: w["x0"],
                        )
                        if _lc_words:
                            _lc_closest = max(_lc_words, key=lambda w: w["x1"])
                            if (
                                f_x0 - _lc_closest["x1"] <= LEFT_COLON_MAX_GAP
                                and _lc_closest["text"].rstrip().endswith(":")
                            ):
                                _lc_chain = [_lc_closest]
                                for _w in reversed(_lc_words):
                                    if _w is _lc_closest:
                                        continue
                                    if _lc_chain[-1]["x0"] - _w["x1"] <= LEFT_COLON_CHAIN_GAP:
                                        _lc_chain.append(_w)
                                    else:
                                        break
                                _lc_chain.reverse()
                                left_colon_label = _clean_label(
                                    " ".join(w["text"] for w in _lc_chain[-MAX_LABEL_WORDS:])
                                ) or None

                    # A confirmed left colon-prompt makes this a plain text
                    # field even if it hugs the left margin — override the
                    # positional checkbox fallback for it too.
                    if left_colon_label:
                        is_checkbox = False

                    if is_checkbox:
                        rights = [ff["x0"] for ff in same_row if ff["x0"] >= f_x1]
                        right_bound = min(rights) if rights else f_x1 + MAX_RIGHT_DIST
                        if cell_x1 is not None:
                            right_bound = min(right_bound, cell_x1)
                    else:
                        lefts = [ff["x1"] for ff in same_row if ff["x1"] <= f_x0]
                        left_bound = max(lefts) if lefts else max(0, f_x0 - MAX_LEFT_DIST)
                        if cell_x0 is not None:
                            left_bound = max(left_bound, cell_x0)

                    # ── "Label ☐" layout pre-check ──────────────────────────
                    # Some forms print the option label BEFORE the checkbox
                    # widget ("Non-Urgent ☐"), not after ("☐ Male"). Detect by:
                    #  a) A word sits very close (≤ 15 px) to the widget LEFT edge
                    #  b) No printed glyph in that gap
                    #  c) The word is NOT within another same-row checkbox's
                    #     right-scan zone (which would mean it's that widget's
                    #     option label, not ours — e.g. "therapy" sitting between
                    #     "☐ New therapy" and "☐ Continuation of therapy")
                    # When conditions are met, chain left through consecutive close
                    # words (gap ≤ 12 px) to capture multi-word labels.
                    LABEL_BOX_MAX_GAP = 15   # px: widget←text gap threshold
                    WORD_CHAIN_GAP    = 12   # px: max between chained words

                    label = ""
                    source = "name"
                    use_left_label = False

                    # A composite split field already has its ONE shared label
                    # resolved above (left-of-run prompt, else header above) —
                    # apply it verbatim and skip the per-box side/above scan so
                    # every box reads identically and no bare "/" survives to be
                    # section-prefixed into noise like "CASE INFORMATION - /".
                    if is_composite and composite_label:
                        label = composite_label
                        source = "geometry"
                        use_left_label = True
                    elif left_colon_label:
                        label = left_colon_label
                        source = "geometry"
                        use_left_label = True

                    _lb_left_bound = f_x0 - 150  # generous window for chain search
                    if cell_x0 is not None:
                        _lb_left_bound = max(_lb_left_bound, cell_x0)

                    if is_checkbox:
                        _lb_cands = sorted(
                            [
                                w for w in words
                                if _word_on_row(w, f_top, f_bottom)
                                and w["x0"] >= _lb_left_bound
                                and w["x1"] <= f_x0 + 2
                                and not _is_glyph_word(w["text"])
                            ],
                            key=lambda w: w["x0"],
                        )
                        if _lb_cands:
                            _closest = max(_lb_cands, key=lambda w: w["x1"])
                            _gap = f_x0 - _closest["x1"]
                            # (b) No printed glyph between closest word and widget
                            _gap_glyphs = [
                                w for w in words
                                if _word_on_row(w, f_top, f_bottom)
                                and _closest["x1"] <= w["x0"] <= f_x0
                                and _is_glyph_word(w["text"])
                            ]
                            # (a2) The closest word must not end in ":" — that
                            # marks a shared QUALIFIER/header for the whole
                            # checkbox row (e.g. "...Credentials (Select
                            # One):", "Sex:"), not this individual checkbox's
                            # own "Label ☐"-style option text. Such qualifiers
                            # are handled separately by Part B below and must
                            # not be swallowed here as this widget's label —
                            # this matters most for the FIRST checkbox in a
                            # row, which sits right after the qualifier with
                            # no other checkbox in between to naturally rule
                            # it out.
                            _closest_is_qualifier = _closest["text"].rstrip().endswith(":")
                            # (c) Closest word must not "belong" to an earlier
                            # same-row checkbox instead of ours. Decide by
                            # DISTANCE rather than a fixed 450px window: the
                            # word belongs to the nearest PREVIOUS checkbox
                            # only if that widget is actually closer to the
                            # word than our own widget is (e.g. in a dense
                            # multi-column grid — "MedicalAdmit[] Acupuncture[]
                            # OccupationalTherapy[]" — "Acupuncture" sits far
                            # (~114px) from "MedicalAdmit"'s box but right next
                            # to (~1px) its own box, so it must stay ours; a
                            # fixed 450px window wrongly disqualified it).
                            _prev_cbs = [
                                ff for ff in page_fields[pnum]
                                if ff is not field
                                and "/Btn" in ff.get("type", "")
                                and _fields_same_row(ff, field)
                                and ff["x1"] <= _closest["x0"]  # prev widget to the left
                            ]
                            TIE_MARGIN = 5  # px: near-equal gaps are noise, not signal
                            if _prev_cbs:
                                _nearest_prev = max(_prev_cbs, key=lambda ff: ff["x1"])
                                _dist_to_prev = _closest["x0"] - _nearest_prev["x1"]
                                # Ties (or near-ties, within TIE_MARGIN) default
                                # to "belongs to prev" — i.e. the ordinary
                                # "☐ Label" reading order — since that
                                # convention is far more common than
                                # "Label ☐"; a near-tie is exactly what a
                                # tightly-packed "☐ Urgent ☐ Standard" row
                                # produces ("Urgent" sits ~equidistant from
                                # both boxes, often by sub-pixel amounts that
                                # can fall on either side of a strict <
                                # comparison), so this must be a tolerant
                                # comparison rather than an exact one, or it
                                # wrongly steals the word as the FOLLOWING
                                # widget's own label.
                                _closest_is_prev_option = _dist_to_prev <= _gap + TIE_MARGIN
                            else:
                                _closest_is_prev_option = False
                            # (d) A real (non-glyph) word already sits on the
                            # widget's OWN right side, within its normal
                            # right-scan zone — i.e. this is NOT a "Label ☐"
                            # form where the right side is empty, but a
                            # "☐ Option" row (or a row-lead-in header case,
                            # e.g. "Review Type Requested [ ] Standard") whose
                            # real option text is to the right. Taking the
                            # left text here would steal a shared row header
                            # as this checkbox's own label and hide its
                            # actual option. Gating on right-emptiness keeps
                            # genuine "Label ☐" forms (no right-side text)
                            # unaffected.
                            _has_right_option = any(
                                _word_on_row(w, f_top, f_bottom)
                                and w["x0"] >= f_x1 - 5
                                and w["x0"] <= right_bound
                                and not _is_glyph_word(w["text"])
                                for w in words
                            )
                            if (
                                _gap <= LABEL_BOX_MAX_GAP
                                and not _gap_glyphs
                                and not _closest_is_prev_option
                                and not _closest_is_qualifier
                                and not _has_right_option
                            ):
                                # Chain LEFT: include consecutive words with gap ≤ 12px
                                _chain = [_closest]
                                for _w in reversed(_lb_cands):
                                    if _w is _closest:
                                        continue
                                    if _chain[-1]["x0"] - _w["x1"] <= WORD_CHAIN_GAP:
                                        _chain.append(_w)
                                    else:
                                        break
                                _chain.reverse()
                                _left_label = _clean_label(
                                    " ".join(w["text"] for w in _chain[-CHECKBOX_LABEL_WORDS:])
                                )
                                if _left_label:
                                    label = _left_label
                                    source = "geometry"
                                    use_left_label = True

                    # ── Side scan ───────────────────────────────────────────
                    if not use_left_label:
                        candidates = []
                        # A composite split field (date/SSN/phone) has its
                        # shared label ABOVE the whole run, never to the side
                        # (only separator glyphs sit between its boxes), so
                        # skip the side scan entirely and let the ABOVE scan
                        # below read the merged-span header.
                        _scan_words = [] if (is_composite and not is_checkbox) else words
                        for w in _scan_words:
                            if not _word_on_row(w, f_top, f_bottom):
                                continue
                            if is_checkbox:
                                # Word extraction sometimes merges the glyph
                                # (☐/□/etc.) into the option text that follows
                                # it with no gap ("□Surgery/Procedure"), which
                                # otherwise starts at/before the widget's own
                                # left edge and gets excluded below — losing
                                # the checkbox's OWN label and letting the
                                # scan fall through to the next column's word
                                # instead. Recognize it by requiring the word
                                # to actually overlap the widget's own x-span
                                # (not just any word left of it) and start
                                # with a glyph character.
                                _own_glyph_word = (
                                    w["x0"] <= f_x0 + 2
                                    and w["x1"] >= f_x0 - 2
                                    and w["text"][:1] in _GLYPH_CHARS
                                    and not _is_glyph_word(w["text"])
                                )
                                if w["x0"] < f_x1 - 5 and not _own_glyph_word:
                                    continue
                                if w["x0"] > right_bound:
                                    continue
                            else:
                                if w["x1"] > f_x0 + 5:
                                    continue
                                if w["x0"] < left_bound:
                                    continue
                            candidates.append(w)

                        if candidates:
                            candidates.sort(key=lambda w: w["x0"])
                            if is_checkbox:
                                # Extract the FIRST option run (stop at next
                                # glyph), capped at CHECKBOX_LABEL_WORDS so a
                                # checkbox whose option text runs into a long
                                # following sentence/label doesn't swallow it
                                # (e.g. "Continuation of therapy (approximate
                                # date therapy initiated ...").
                                # x0s of OTHER checkbox widgets sitting to our
                                # right within a small vertical band — a word
                                # with one of these just to its left begins a
                                # SEPARATE option even when that option's box
                                # is drawn a row off (so no glyph separates them
                                # on this baseline, e.g. cigna "☐ Non Urgent
                                # Urgent" whose 2nd box sits one row above).
                                # Boxes that separate our option from the NEXT
                                # one when the next option's box was drawn OFF its
                                # own text baseline (the cigna case: "☐ Non Urgent
                                # Urgent" whose 2nd box sits a row above the text).
                                # Restrict to ORPHAN boxes — a box a row above
                                # (6 < Δtop ≤ BAND) that has NO label text of its
                                # own on its baseline. A box that belongs to a
                                # normal option row above (e.g. a vertical/
                                # horizontal specialty list) DOES carry adjacent
                                # text, so it's excluded here and legitimate
                                # multi-word options ("Internal Medicine",
                                # "Home Infusion") are left intact.
                                def _box_is_orphan(bx: float, by: float) -> bool:
                                    return not any(
                                        not _is_glyph_word(w["text"])
                                        and abs(w["top"] - by) <= 5
                                        and (bx - 4) <= w["x0"] <= (bx + 45)
                                        for w in words
                                    )
                                _next_opt_box_x0s = [
                                    ff["x0"] for ff in page_fields[pnum]
                                    if ("/Btn" in ff.get("type", "") or ff["x0"] < CHECKBOX_X_THRESHOLD)
                                    and ff["x0"] > f_x0 + 5
                                    and 6 < (f_top - ff["y"]) <= CHECKBOX_OPT_BOX_BAND
                                    and _box_is_orphan(ff["x0"], ff["y"])
                                ]
                                opt_words: list[dict] = []
                                for w in candidates[:8]:
                                    if _is_glyph_word(w["text"]):
                                        if opt_words:
                                            break
                                        continue
                                    # A word starting with a glyph char but
                                    # also carrying real text (e.g. a tightly
                                    # packed "☐Urgent" with no gap) is the
                                    # NEXT checkbox's own glued glyph+label
                                    # bleeding across `right_bound` by a
                                    # sub-pixel amount, not a continuation of
                                    # OUR option run — stop here rather than
                                    # swallowing it too (unless it's genuinely
                                    # our OWN glued glyph+label, i.e. it
                                    # overlaps our own widget's x-span).
                                    _leading_glyph_not_ours = (
                                        w["text"][:1] in _GLYPH_CHARS
                                        and not (w["x0"] <= f_x0 + 2 and w["x1"] >= f_x0 - 2)
                                    )
                                    if _leading_glyph_not_ours:
                                        break
                                    # Stop before a word that has ANOTHER
                                    # checkbox's box just to its left: it's the
                                    # next option, not a continuation of ours.
                                    if opt_words and any(
                                        w["x0"] - CHECKBOX_OPT_BOX_LEFT <= bx <= w["x0"] + 2
                                        for bx in _next_opt_box_x0s
                                    ):
                                        break
                                    opt_words.append(w)
                                    if len(opt_words) >= CHECKBOX_LABEL_WORDS:
                                        break
                                label_words = opt_words or candidates[:6]
                            else:
                                label_words = candidates[-MAX_LABEL_WORDS:]

                            raw_label = " ".join(w["text"] for w in label_words).strip()
                            label = _clean_label(raw_label)

                            # ── Part B: checkbox group qualifier ("Sex: Male") ──
                            # Look to the LEFT of the ENTIRE checkbox group on this
                            # row for a short qualifier like "Sex". Include the
                            # current field's x0 so inter-checkbox text (e.g. the
                            # "Male" label sitting between ☐ and ☐ Female) is not
                            # captured as the qualifier. Skipped when a row
                            # lead-in header was already detected (e.g. "This
                            # medication will be administered by") — that
                            # text becomes the shared `group` below instead,
                            # so merging it in here too would duplicate it
                            # into every option's own label.
                            if is_checkbox and label and not row_lead_group:
                                other_cb_x0s = [
                                    ff["x0"] for ff in same_row
                                    if "/Btn" in ff.get("type", "") or ff["x0"] < CHECKBOX_X_THRESHOLD
                                ]
                                leftmost_x0 = min([f_x0] + other_cb_x0s) if other_cb_x0s else f_x0
                                qual_left_bound = cell_x0 if cell_x0 is not None else 0.0
                                qual_words = [
                                    w for w in words
                                    if _word_on_row(w, f_top, f_bottom)
                                    and w["x1"] <= leftmost_x0 - 2
                                    and w["x0"] >= qual_left_bound
                                    and not _is_glyph_word(w["text"])
                                ]
                                if qual_words:
                                    qual_words.sort(key=lambda w: w["x0"])
                                    # A real qualifier ("Sex:", "DOB:") is
                                    # printed tight against the checkbox
                                    # group it introduces — bound the gap so
                                    # unrelated text sitting further left on
                                    # the same row (e.g. an instructional
                                    # sentence "...Mark ✓ or X:" ending well
                                    # before an option far to its right)
                                    # can't be mistaken for one.
                                    QUAL_MAX_GAP = 20  # px: qualifier<-checkbox-group gap threshold
                                    _qual_gap = leftmost_x0 - qual_words[-1]["x1"]
                                    qual = _clean_label(
                                        " ".join(w["text"] for w in qual_words[-3:])
                                    )
                                    # A real qualifier ("Sex", "DOB", "Age")
                                    # always has at least one substantive
                                    # word — guards against a trailing
                                    # fragment of unrelated instructional
                                    # text bleeding in (e.g. "...Mark ✓ or
                                    # X:" printed on the same row as an
                                    # option far to its right), which after
                                    # glyph-stripping and taking only the
                                    # last 3 words leaves junk like "or X".
                                    _qual_has_real_word = any(
                                        len(t) >= 3 for t in qual.split()
                                    )
                                    # Guards — skip if qualifier is long/spurious
                                    # or contains ":" (form-field labels like
                                    # "Name:", "DOB:" are not group qualifiers)
                                    if (
                                        qual
                                        and qual.lower() != label.lower()
                                        and ":" not in qual
                                        and _qual_has_real_word
                                        and _qual_gap <= QUAL_MAX_GAP
                                        and len(f"{qual}: {label}") <= 40
                                    ):
                                        label = f"{qual}: {label}"

                            if label:
                                source = "geometry"
                            else:
                                label = field["name"]
                                source = "name"
                        else:
                            # ── Table-header fallback: look ABOVE ───────────
                            ABOVE_MIN = 4
                            ABOVE_MAX = 50
                            EDGE_TOL  = 2
                            # For a composite split field, look above the FULL
                            # merged run so all boxes read the same shared
                            # header (e.g. "Date of Birth (mm/dd/yyyy)" over
                            # [MM] / [DD] / [YYYY]) instead of a per-box shard.
                            scan_x0 = comp_x0 if is_composite else f_x0
                            scan_x1 = comp_x1 if is_composite else f_x1

                            above_candidates = [
                                w for w in words
                                if ABOVE_MIN <= (f_top - w["top"]) <= ABOVE_MAX
                                and w["x1"] > scan_x0 - EDGE_TOL
                                and w["x0"] < scan_x1 + EDGE_TOL
                            ]

                            if above_candidates:
                                nearest_top = max(w["top"] for w in above_candidates)
                                ROW_SNAP = 6
                                nearest_row = [
                                    w for w in above_candidates
                                    if abs(w["top"] - nearest_top) <= ROW_SNAP
                                ]
                                nearest_row.sort(key=lambda w: w["x0"])
                                raw_above = " ".join(w["text"] for w in nearest_row).strip()
                                label = _clean_label(raw_above)

                                # ── Part C: two-level header join (guarded) ─
                                # Look one band higher for a WIDER header
                                # (e.g. "Service Provider or Facility").
                                # Only prefix when the super-row is meaningfully
                                # wider than this column (≥ 2× field width).
                                # Skipped for a composite split field: its
                                # header directly above (e.g. "Date of Birth
                                # (mm/dd/yyyy)") is already the complete label,
                                # and the row above it is a DIFFERENT field's
                                # prompt (e.g. "Patient Name (Last, First)"),
                                # not a shared category — joining it produces
                                # noise like "Patient Name (Last, First) -
                                # Date of Birth (mm/dd/yyyy)".
                                if label and not is_composite:
                                    super_min = nearest_top - ABOVE_MAX
                                    super_max = nearest_top - ABOVE_MIN
                                    super_cands = [
                                        w for w in words
                                        if super_min <= w["top"] <= super_max
                                        and w["x1"] > scan_x0 - EDGE_TOL
                                        and w["x0"] < scan_x1 + EDGE_TOL
                                    ]
                                    if super_cands:
                                        s_top = max(w["top"] for w in super_cands)
                                        super_row = [
                                            w for w in super_cands
                                            if abs(w["top"] - s_top) <= ROW_SNAP
                                        ]
                                        s_x0 = min(w["x0"] for w in super_row)
                                        s_x1 = max(w["x1"] for w in super_row)
                                        field_width = max(1, f_x1 - f_x0)
                                        super_width = s_x1 - s_x0
                                        super_row.sort(key=lambda w: w["x0"])
                                        raw_super = " ".join(w["text"] for w in super_row).strip()
                                        # A row ending in ":" is a FIELD PROMPT
                                        # for some OTHER field (e.g. "Physician
                                        # Name:" sitting two bands above this
                                        # field's own "NPI" prompt) — not a
                                        # shared, wider category header. Joining
                                        # it in would wrongly weld two unrelated
                                        # fields' prompts into one label. A real
                                        # multi-level header (e.g. "Provider
                                        # Information" spanning "Name"/"NPI"/
                                        # "Phone" columns below it) never itself
                                        # ends in ":".
                                        is_field_prompt = raw_super.rstrip().endswith(":")
                                        if super_width >= field_width * 2 and not is_field_prompt:
                                            super_text = _clean_label(raw_super)
                                            if super_text and super_text.lower() != label.lower():
                                                label = f"{super_text} - {label}"

                                if not label:
                                    label = field["name"]
                                    source = "name"
                                else:
                                    source = "table-header" if is_grid_cell else "cell-group"
                            else:
                                label = field["name"]
                                source = "name"

                    # ── Checkbox group-header detection ─────────────────────
                    # Distinct from Part B (same-row short qualifier merged
                    # directly into the label, e.g. "Sex: Male"). This handles
                    # group headers that sit ABOVE a checkbox row/block (e.g.
                    # "Type of Transplant" above Lung/Heart/Kidney checkboxes,
                    # "Most Recent Transplant Payer (check one)" above a wider
                    # option row) or a long qualifying QUESTION to the left that
                    # ends in "?"/":" (e.g. "Is the member diagnosed with Autism
                    # Spectrum Disorder?"). Stored separately as entry["group"];
                    # the clean option label is left untouched.
                    # NOTE: gated on the actual AcroForm /Btn type (not the
                    # is_checkbox x0-heuristic used elsewhere), since that
                    # heuristic false-positives on text fields sitting near the
                    # left margin (x0 < 80) — running group-header detection
                    # for those would corrupt their (correct) textbox label.
                    group: str | None = None
                    is_real_checkbox = "/Btn" in field.get("type", "")
                    if is_real_checkbox and label and ":" not in label:
                        # (0) Column-grid header — checked FIRST since it's
                        # the most specific/reliable signal when present (a
                        # multi-row column of checkboxes with a real "column
                        # header" — see Part A2 above). Only fall back to the
                        # same-row-block (a)/(b) heuristics below when no
                        # column header governs this checkbox.
                        group = _nearest_column_group(pnum, f_top, f_x0, f_x1)

                        # (0b) Cell header — for bordered grids whose column
                        # headers DON'T end in ":" (e.g. "Ambulatory/Outpatient
                        # Services" over its own printed cell), fall back to
                        # the enclosing cell's topmost text-only line(s) as
                        # the group, when that cell holds 2+ checkboxes.
                        if not group:
                            group = _cell_header_group(pnum, f_top, f_bottom, f_x0, f_x1)

                    if is_real_checkbox and label and ":" not in label and not group:
                        other_cb = [
                            ff for ff in same_row
                            if "/Btn" in ff.get("type", "")
                        ]
                        grp_x0 = min([f_x0] + [ff["x0"] for ff in other_cb])
                        grp_x1 = max([f_x1] + [ff["x1"] for ff in other_cb])

                        def _is_group_headerish(text: str) -> bool:
                            # Reject text that looks like a data-entry field
                            # label or a stray fragment rather than a standalone
                            # group header:
                            #  - underline/placeholder runs ("____")
                            #  - phone/number cells ("Phone#: (___)")
                            #  - anything with a mid-string colon+content
                            #  - short single-word fragments ("How")
                            if not text or not (5 <= len(text) <= 70):
                                return False
                            if "_" in text or "#" in text:
                                return False
                            # A colon is fine only as the trailing char (header:)
                            if ":" in text.rstrip(":"):
                                return False
                            # Require a real header: multi-word, or reasonably long
                            if " " not in text and len(text) < 8:
                                return False
                            return True

                        def _row_has_field(y_top: float, y_bottom: float) -> bool:
                            # A genuine group header sits on a text-only row.
                            # If any AcroForm field shares that row, the text is
                            # a field label (previous data row), not a header.
                            return any(
                                _row_overlap(ff["y"], ff.get("y_bottom", ff["y"]), y_top, y_bottom)
                                for ff in page_fields[pnum]
                            )

                        # (a) Look ABOVE the whole checkbox row for a header
                        # whose x-range overlaps the row's span, on a text-only
                        # row (no fields) that is not itself a field label.
                        GROUP_ABOVE_MIN = 4
                        GROUP_ABOVE_MAX = 60
                        GROUP_ROW_SNAP  = 6
                        # Never scan above this checkbox's own printed cell —
                        # text above that ruling belongs to a different table
                        # row/section entirely (e.g. a running page header),
                        # not a header for THIS cell. Only apply this floor
                        # when the field sits in a FULLY bordered cell (both
                        # vertical AND horizontal rulings found) — a lone
                        # horizontal rect is very often just a decorative
                        # underline/rule (e.g. under a signature line), not a
                        # real table top edge, and would wrongly cut off a
                        # genuine group header sitting a bit further above.
                        above_scan_floor = (
                            cell_top - CELL_TOL
                            if cell_top is not None and cell_x0 is not None and cell_x1 is not None
                            else None
                        )
                        above_group_cands = [
                            w for w in words
                            if GROUP_ABOVE_MIN <= (f_top - w["top"]) <= GROUP_ABOVE_MAX
                            and (above_scan_floor is None or w["top"] >= above_scan_floor)
                            and w["x1"] > grp_x0 - 10
                            and w["x0"] < grp_x1 + 10
                            and not _is_glyph_word(w["text"])
                        ]
                        if above_group_cands:
                            g_top = max(w["top"] for w in above_group_cands)
                            g_row = [
                                w for w in above_group_cands
                                if abs(w["top"] - g_top) <= GROUP_ROW_SNAP
                            ]
                            g_row.sort(key=lambda w: w["x0"])
                            g_bottom = max(w.get("bottom", w["top"]) for w in g_row)
                            g_text = _clean_label(" ".join(w["text"] for w in g_row))
                            if (
                                _is_group_headerish(g_text)
                                and g_text.lower() != label.lower()
                                and not _row_has_field(g_top, g_bottom)
                            ):
                                group = g_text

                        # (b) Long same-row LEFT question ending in "?" / ":"
                        # (relaxed vs. Part B's short-qualifier guard — allows
                        # colons since it is stored separately, never merged
                        # into the display label). Must not itself sit in
                        # another checkbox's option zone.
                        if not group:
                            left_words = [
                                w for w in words
                                if _word_on_row(w, f_top, f_bottom)
                                and w["x1"] <= grp_x0 - 2
                                and not _is_glyph_word(w["text"])
                            ]
                            if left_words:
                                left_words.sort(key=lambda w: w["x0"])
                                long_left = _clean_label(
                                    " ".join(w["text"] for w in left_words[-14:])
                                )
                                stem = long_left.rstrip(" :?")
                                if (
                                    long_left
                                    and (long_left.endswith("?") or long_left.endswith(":"))
                                    and len(stem) >= 8
                                    and stem.lower() != label.lower()
                                    and "_" not in stem
                                ):
                                    group = stem

                        # (c) Sandwiched Yes/No question — some forms print the
                        # shared question on its OWN row physically BETWEEN the
                        # stacked ☐Yes / ☐No options (question to the left, boxes
                        # to the right), so it is neither above the row nor on
                        # the same row as either option. For a bare Yes/No box
                        # with no group yet, scan a small vertical band (above OR
                        # below) to the left for a line ending in "?" and adopt
                        # it as the shared group.
                        if not group and re.sub(r"[^a-z]", "", label.lower()) in ("yes", "no", "y", "n"):
                            QSANDWICH_BAND = 30  # px from the checkbox center
                            cb_center = (f_top + f_bottom) / 2.0
                            band_words = [
                                w for w in words
                                if abs(((w["top"] + w.get("bottom", w["top"])) / 2.0) - cb_center) <= QSANDWICH_BAND
                                and w["x0"] < grp_x0
                                and not _is_glyph_word(w["text"])
                            ]
                            q_rows: dict[int, list[dict]] = defaultdict(list)
                            for w in band_words:
                                q_rows[round(w["top"] / 4) * 4].append(w)
                            for bucket in sorted(q_rows, key=lambda b: abs(b - cb_center)):
                                rw = sorted(q_rows[bucket], key=lambda w: w["x0"])
                                q_text = _clean_label(" ".join(w["text"] for w in rw)) or ""
                                q_stem = q_text.rstrip(" :?")
                                if q_text.endswith("?") and len(q_stem) >= 10 and "_" not in q_stem:
                                    group = q_stem
                                    break

                        # (c2) Row lead-in header, computed earlier — a real
                        # printed header to the LEFT of the leftmost box in
                        # this row-group (e.g. "Review Type Requested",
                        # "This medication will be administered by"). Takes
                        # priority over the bare synthetic id below since it
                        # is real printed text, not a fallback placeholder.
                        if not group and row_lead_group:
                            group = row_lead_group

                        # (d) No real header text anywhere for this checkbox,
                        # but it's co-located on one row with other checkbox
                        # options (e.g. a bare "Inpatient Outpatient Provider
                        # Office Home Day Surgery" row with nothing above/left
                        # to explain it) — give the whole row ONE shared
                        # synthetic id rather than leaving them ungrouped (and
                        # certainly never inheriting an unrelated label from
                        # elsewhere on the page).
                        if not group:
                            row_cbs = [field] + [ff for ff in same_row if "/Btn" in ff.get("type", "")]
                            if len(row_cbs) >= 2:
                                # Page-scoped for the same reason as the row-lead
                                # and composite caches above — generic
                                # positional field names repeat per page.
                                row_key = (pnum, tuple(sorted({self._widget_key(ff) for ff in row_cbs})))
                                cached = synthetic_row_groups.get(row_key)
                                if cached is None:
                                    _synthetic_group_counter[0] += 1
                                    cached = f"group_{_synthetic_group_counter[0]}"
                                    synthetic_row_groups[row_key] = cached
                                group = cached

                    # ── Structured override: Yes/No radio whose parent field
                    # name IS the printed question ───────────────────────────
                    # Some forms embed the exact printed question as the
                    # AcroForm field's own /T (e.g. "Is member currently
                    # treated on this medication?"), with each option widget
                    # carrying a clean "yes"/"no" export value. Geometry can
                    # fail badly here — e.g. two overlapping printed text
                    # layers interleaving into garbage like
                    # "MExepdeiccatetdio" — but this structured AcroForm data
                    # is unambiguous, so trust it over whatever geometry
                    # produced for exactly this well-defined pattern. Forms
                    # with meaningless export values ("1", "Choice_A") or
                    # system-style field names ("Btn3[0]") are untouched and
                    # keep using the geometry-based logic above.
                    if (
                        field.get("_radio_group")
                        and _is_yesno_export(field.get("export_value"))
                        and _is_question_like_name(field["name"])
                    ):
                        label = field["export_value"].strip().capitalize()
                        source = "export"
                        group = field["name"]

                    # ── Structured override: radio group whose export values
                    # ARE the option labels (e.g. Sex → /Male, /Female) ───────
                    # When each option carries a DISTINCT, word-like export
                    # value (not On/Off/Yes/No/1/undefined) the export IS the
                    # cleanest per-option label. This form's Sex boxes share
                    # /T "Sex" and /TU "Sex" (the group), with the real option
                    # only in the export state — geometry just re-grabs "Sex".
                    # Use the shared /TU (or name) as the group so the options
                    # stay tied together.
                    elif (
                        field.get("_radio_group")
                        and _is_meaningful_export(field.get("export_value"))
                    ):
                        ev = field["export_value"].strip()
                        label = ev[:1].upper() + ev[1:]
                        source = "export"
                        if field.get("tu"):
                            group = field["tu"]

                    # ── Structured override: text field mis-placed a row off
                    # from its own printed prompt ────────────────────────────
                    # Some forms draw a text field's rect a few px into the
                    # WRONG printed row (e.g. a "How Long" field's box
                    # overlapping the row above its own "How Long?___"
                    # prompt), so the normal same-row scan above grabbed an
                    # unrelated neighboring sentence instead. Only trust this
                    # when the resolved label shares NOT EVEN ONE word with
                    # the field's own name — a real geometry match (even an
                    # imperfect one) almost always overlaps the name at
                    # least partially, so this only fires on a clear miss.
                    if (
                        not is_checkbox
                        and label
                        and source in ("geometry", "table-header", "cell-group")
                    ):
                        name_tokens = [t for t in (_norm_token(w) for w in field["name"].split()) if t]
                        label_tokens = {_norm_token(w) for w in label.split()}
                        if name_tokens and not (label_tokens & set(name_tokens)):
                            matched = _find_name_matched_label(field["name"], f_top, words)
                            if matched:
                                label = matched
                                source = "name-match"

                    # /TU (alternate field name) is an author-provided,
                    # per-widget caption — authoritative when present. Trust it
                    # over geometry, which can misfire on densely-boxed or
                    # duplicate-named forms (e.g. this aetna form names every
                    # box "0"/"1"/"2" but carries /TU="Primary ICD code" etc.).
                    # Skip radio-group options: their label/group is already
                    # resolved from export values + the printed question above,
                    # and a parent /TU would be the shared question, not the
                    # per-option "Yes"/"No". Also require _acro: only a real
                    # widget's /TU provably matches its box (CV-rebound boxes
                    # carry an unreliable /TU — see _widget_key).
                    if field.get("_acro") and field.get("tu") and not field.get("_radio_group"):
                        label = field["tu"]
                        source = "acroform-tu"

                    section = _nearest_section(pnum, f_top, f_x0, f_x1)
                    # Give every field inside a confirmed table region a
                    # shared identity (its section header, or a synthetic
                    # "table_N" when there's none) so autofill/consumers know
                    # its rows/columns belong together as one table.
                    table = _table_name_for(pnum, table_idx, table_region) if table_region is not None else None
                    # Field names can repeat across pages (e.g. a "Beneficiary
                    # Name" continuation-page header widget mirrors the same
                    # AcroForm field on every page). Keep the FIRST
                    # successfully-resolved occurrence rather than letting a
                    # later page's (often worse — e.g. a shared banner over
                    # two side-by-side fields) computation silently clobber it.
                    wkey = self._widget_key(field)
                    existing = result.get(wkey)
                    if existing is None or existing.get("source") == "name":
                        result[wkey] = {
                            "label": label, "source": source, "section": section, "group": group,
                            "table": table,
                        }

                # ── Disambiguate duplicate labels ────────────────────────────
                # Prefix section when left-row context is unavailable so that
                # repeated cells across sections become distinguishable.
                # Keyed by widget-key (matches `result`) so radio-group option
                # widgets map back to their own widget rather than colliding.
                name_to_field = {self._widget_key(f): f for f in fields_info}

                groups: dict[tuple, list[str]] = defaultdict(list)
                for fname, entry in result.items():
                    if entry["source"] in ("geometry", "table-header", "cell-group"):
                        # A field inside a confirmed table region SHOULD share
                        # its column header with the same column's other rows
                        # (e.g. "Current Score" on every row of the IBX
                        # Section D grid). That repetition is correct, not a
                        # collision — the rows are already distinguished by the
                        # `table` identity — so don't section-prefix them (which
                        # would turn "Current Score" into "SECTION D … - Current
                        # Score").
                        if entry.get("table"):
                            continue
                        f = name_to_field.get(fname)
                        if f is None:
                            continue
                        key = (f["page"], entry["label"].strip().lower())
                        groups[key].append(fname)

                for (pnum, _lbl), names in groups.items():
                    if len(names) < 2:
                        continue
                    # Only disambiguate SHORT labels ("Name", "NDC #", "Phone").
                    # Long labels are already distinctive, and prefixing them
                    # produces noise like "New therapy - Continuation of therapy
                    # (approximate date therapy initiated...".
                    if len(_lbl) > 25:
                        continue
                    words = page_words.get(pnum, [])
                    for fname in names:
                        f = name_to_field[fname]
                        f_top, f_x0 = f["y"], f["x0"]
                        f_bottom = f.get("y_bottom", f_top)
                        cur = result[fname]["label"]

                        # Checkboxes/radios: a same-row LEFT-word scan almost
                        # always lands on the SIBLING option's own printed
                        # text (e.g. "Yes" sitting immediately left of "No"'s
                        # widget) rather than a real qualifying label — that
                        # produced bogus merges like "Yes - No". A checkbox's
                        # true qualifying context is already captured (as
                        # `group`, e.g. "1. Can this beneficiary…"), so skip
                        # the word-scan for checkboxes and go straight to the
                        # section-prefix fallback below.
                        is_cb = "/Btn" in f.get("type", "")

                        if not is_cb:
                            lefts = [
                                ff["x1"] for ff in page_fields[pnum]
                                if ff is not f
                                and _fields_same_row(ff, f)
                                and ff["x1"] <= f_x0
                            ]
                            ctx_left_bound = max(lefts) if lefts else 0
                            ctx_words = [
                                w for w in words
                                if _word_on_row(w, f_top, f_bottom)
                                and w["x1"] <= f_x0 + 5
                                and w["x0"] >= ctx_left_bound
                                and not _is_glyph_word(w["text"])
                            ]
                            if ctx_words:
                                ctx_words.sort(key=lambda w: w["x0"])
                                ctx = _clean_label(" ".join(w["text"] for w in ctx_words[-3:]))
                                if ctx and ctx.lower() != cur.strip().lower():
                                    result[fname]["label"] = f"{ctx} - {cur}"
                                    continue
                        # No left row context (or a checkbox) — fall back to
                        # section prefix. (Group context is intentionally NOT
                        # merged into the display label here — it is surfaced
                        # only via the separate `group` attribute and the AI
                        # prompt, so a noisy group heuristic can never corrupt
                        # a label.)
                        sec = result[fname].get("section")
                        if sec and sec.lower() not in cur.lower():
                            result[fname]["label"] = f"{sec} - {cur}"

        except Exception as e:
            print(f"  ⚠️  pdfplumber label extraction failed: {e}")
            return {
                self._widget_key(f): {
                    "label": _humanize_field_name(f["name"]),
                    "source": "name",
                    "section": None,
                    "group": None,
                }
                for f in fields_info
            }

        # ── Humanize remaining name-fallback labels ──────────────────────────
        for entry in result.values():
            if entry["source"] == "name":
                entry["label"] = _humanize_field_name(entry["label"])

        return result

    # ------------------------------------------------------------------
    # Vision label resolver (Gemini) for unresolved fields
    # ------------------------------------------------------------------

    def _label_unresolved_with_vision(
        self,
        pdf_path: str,
        fields_info: list[dict],
        unresolved_keys: set[str],
    ) -> dict[str, str]:
        """
        For fields whose label geometry couldn't resolve, render the page and
        ask Gemini to read the printed label/caption nearest each field's box.

        ``unresolved_keys`` are ``_widget_key`` values (name, or name-scoped by
        export/TU) — NOT raw field names — so checkbox/radio option widgets and
        forms that reuse one field name across many widgets are handled too.

        Fields are sent to the model under stable synthetic ids ("f0","f1"…),
        which avoids leaking control characters from the widget keys into the
        prompt and guarantees a clean round-trip.

        Returns ``dict[widget_key -> label_string]`` for resolved fields only.
        Uses self.api_key / self.base_url / self.model via the openai SDK.
        """
        if not unresolved_keys or not self.api_key or self.api_key in ("-", ""):
            return {}

        try:
            import base64
            import fitz  # PyMuPDF

            DPI = 120
            scale = DPI / 72.0

            # Assign each unresolved field a stable synthetic id, grouped by page.
            id_to_key: dict[str, str] = {}
            by_page: dict[int, list[tuple[str, dict]]] = {}
            n = 0
            for f in fields_info:
                wkey = self._widget_key(f)
                if wkey not in unresolved_keys:
                    continue
                fid = f"f{n}"
                n += 1
                id_to_key[fid] = wkey
                by_page.setdefault(f["page"], []).append((fid, f))
            if not id_to_key:
                return {}

            doc = fitz.open(pdf_path)
            resolved: dict[str, str] = {}

            for pno, page_fields in by_page.items():
                if pno >= doc.page_count:
                    continue
                page = doc[pno]
                pix = page.get_pixmap(dpi=DPI)
                png_bytes = pix.tobytes("png")
                b64 = base64.standard_b64encode(png_bytes).decode()
                data_uri = f"data:image/png;base64,{b64}"

                fields_text = "\n".join(
                    f"- {fid} : [{round(f['x0']*scale,1)}, "
                    f"{round(f['y']*scale,1)}, "
                    f"{round(f['x1']*scale,1)}, "
                    f"{round(f.get('y_bottom', f['y'])*scale,1)}] "
                    f"({'checkbox' if '/Btn' in f.get('type','') else 'text'})"
                    for fid, f in page_fields
                )
                prompt = (
                    "You are reading a PDF form page. Each item below is a form "
                    "field: an id, its pixel bounding box [x0,y0,x1,y1], and its "
                    "kind.\n"
                    "For a TEXT field, return the printed label/prompt describing "
                    "what should be typed there (the caption to its left or above).\n"
                    "For a CHECKBOX field, return the specific option text printed "
                    "immediately beside that box (e.g. 'Urgent', 'Male'), NOT the "
                    "group heading.\n"
                    "Respond ONLY with a JSON object mapping id to label: "
                    "{\"f0\": \"<label text>\"}\n"
                    "Use an empty string if no label is visible.\n\n"
                    f"Fields:\n{fields_text}"
                )

                client = OpenAI(api_key=self.api_key, base_url=self.base_url)
                resp = client.chat.completions.create(
                    model=self.model,
                    temperature=0.0,
                    max_tokens=1200,
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
                import json as _json
                try:
                    parsed = _json.loads(raw)
                    for fid, lbl in parsed.items():
                        if fid in id_to_key and str(lbl).strip():
                            resolved[id_to_key[fid]] = str(lbl).strip()
                except Exception:
                    pass

            doc.close()
            return resolved

        except Exception as e:
            print(f"  ⚠️  Vision label fallback failed: {e}")
            return {}

    # ------------------------------------------------------------------
    # Inspect fillable PDF (geometry + inferred labels — no AI / no OpenAI calls)
    # ------------------------------------------------------------------

    def inspect_fillable_form(
        self,
        fillable_pdf_path: str,
        *,
        ai_labels: bool = False,
        engine: Optional[str] = None,
    ) -> dict:
        """
        List every form field with inferred human-facing label text.
        Used by products to preview mappings before filling and for Layer 3
        transparency tooling.

        The ``engine`` selects HOW fields are found + understood (defaults to
        ``settings.FIELD_DETECTION_ENGINE``):
          - "opencv"    detect boxes from the rendered page with OpenCV (local,
                        no AI); bind to AcroForm widgets so fills work; labels
                        from pdfplumber geometry (or OCR for scanned pages).
          - "vlm_local" local Qwen-VL reads the page for label/section/group.
          - "acroform"  original pypdf-widget + pdfplumber-geometry pipeline.
          - "gemini"    FULL cloud pass: AcroForm gives the boxes/names/values,
                        then the whole page image + every field is sent to
                        Gemini, which returns label/section/group for ALL
                        fields (geometry runs first as a fallback baseline).

        When ai_labels=True (acroform engine only), fields geometry couldn't
        label (source='name') are sent to Gemini vision for a second pass.
        """
        engine = (engine or settings.FIELD_DETECTION_ENGINE or "acroform").lower()

        if engine == "opencv":
            fields_info, label_data = self._inspect_opencv(fillable_pdf_path)
        elif engine == "vlm_local":
            fields_info, label_data = self._inspect_vlm_local(fillable_pdf_path)
        elif engine == "gemini":
            fields_info, label_data = self._inspect_gemini(fillable_pdf_path)
        else:
            fields_info, label_data = self._inspect_acroform(
                fillable_pdf_path, ai_labels=ai_labels
            )

        if not fields_info:
            return {"fields_detected": 0, "fields": []}

        return self._build_inspect_rows(fields_info, label_data)

    def rich_label_data(
        self,
        pdf_path: str,
        fields_info: list[dict],
        *,
        allow_ai: bool = True,
    ) -> dict:
        """Best available per-widget label record for a blank form.

        Geometry alone resolves the printed caption but rarely the *question* a
        checkbox belongs to; the full Gemini pass resolves ``group`` for the
        great majority of them. Extract has always used the richer pass while
        map-building used geometry only, so the canonical side saw strictly
        worse data for the same form.

        Cache-first, and the label cache is keyed by form structure — so a form
        already run through extract costs nothing here.
        """
        try:
            fp = self._label_cache.fingerprint(fields_info, model=self.model or "")
            cached = self._label_cache.get(fp)
            if cached is not None:
                self._postprocess_label_data(fields_info, cached)
                return cached
        except Exception:
            pass

        can_call_ai = (
            allow_ai
            and settings.AI_LABEL_FALLBACK
            and not settings.AI_LOCAL_ONLY
            and bool(self.api_key)
            and self.api_key != "-"
            and bool(self.base_url)
        )
        if can_call_ai:
            try:
                _, label_data = self._inspect_gemini(pdf_path)
                if label_data:
                    return label_data
            except Exception as exc:
                print(f"  ⚠️  rich label pass failed ({exc}); using geometry")

        # Geometry invents positional ids ("group_3") to mark boxes that share a
        # printed row. Those are not questions, so apply the same normalization
        # the Gemini paths get: it nulls the placeholders and confines `group` to
        # checkbox widgets. Documented as idempotent, so safe on any label map.
        label_data = self._extract_labels_for_fields(pdf_path, fields_info)
        try:
            self._postprocess_label_data(fields_info, label_data)
        except Exception as exc:
            print(f"  ⚠️  label post-processing failed ({exc}); using raw geometry")
        return label_data

    # ------------------------------------------------------------------
    # Engine implementations (each returns (fields_info, label_data))
    # ------------------------------------------------------------------
    def _inspect_acroform(
        self, fillable_pdf_path: str, *, ai_labels: bool = False
    ) -> tuple[list[dict], dict]:
        """Original pipeline: pypdf widgets + pdfplumber geometry (+optional Gemini)."""
        fields_info = self._get_fields_with_coords(fillable_pdf_path)
        if not fields_info:
            return [], {}
        label_data = self._extract_labels_for_fields(fillable_pdf_path, fields_info)

        if ai_labels:
            # "Unresolved" = geometry fell back to humanizing the field's own
            # name (label_source=="name"), i.e. AcroForm/geometry found no
            # printed label. These are the fields we hand to Gemini.
            unresolved = {
                wkey for wkey, d in label_data.items()
                if d.get("source") == "name"
            }
            if unresolved:
                vision_labels = self._label_unresolved_with_vision(
                    fillable_pdf_path, fields_info, unresolved
                )
                for wkey, lbl in vision_labels.items():
                    prev = label_data.get(wkey, {})
                    # Keep any section/group/table geometry did find; only the
                    # label + source come from Gemini.
                    label_data[wkey] = {**prev, "label": lbl, "source": "vision"}
        return fields_info, label_data

    def _inspect_gemini(
        self, fillable_pdf_path: str
    ) -> tuple[list[dict], dict]:
        """
        FULL Gemini pass.  AcroForm supplies the widget boxes / names / values
        and the pdfplumber geometry runs first as a baseline; then the WHOLE
        page image plus every field on it is sent to Gemini, which returns a
        label / section / group for each field.  Gemini's answer overrides the
        geometry label/section/group wherever it returns one, so wrong-but-
        present geometry labels (garbled checkbox captions, mis-sectioned
        fields) get corrected — not just the ones geometry missed.

        Falls back to the geometry baseline for any field Gemini omits or on
        any API failure.
        """
        fields_info = self._get_fields_with_coords(fillable_pdf_path)
        if not fields_info:
            return [], {}

        # Cache-first: the label map depends only on the blank form's structure,
        # so a template is sent to Gemini at most once. A hit means no AI call
        # and no PHI egress on this (and every later) request.
        fp = self._label_cache.fingerprint(fields_info, model=self.model or "")
        cached = self._label_cache.get(fp)
        if cached is not None:
            print(f"  ✅  Label cache HIT (fp={fp[:8]}…) — skipping Gemini")
            # Re-run (idempotent) post-processing so section normalization,
            # radio reconciliation and cross-page section carry apply to older
            # cached maps saved before those steps existed.
            self._postprocess_label_data(fields_info, cached)
            return fields_info, cached

        # Geometry baseline first. Gemini overrides it wherever it answers, but
        # for any field Gemini misses (a failed/empty chunk on a dense page) we
        # keep the geometry label/section/group instead of nulling it — nulling
        # everything Gemini omits is strictly worse than the geometry guess.
        label_data = self._extract_labels_for_fields(fillable_pdf_path, fields_info)

        vision = self._label_fields_with_vision_full(fillable_pdf_path, fields_info)

        for f in fields_info:
            wkey = self._widget_key(f)
            info = vision.get(wkey)
            if not (info and info.get("label")):
                continue  # keep the geometry baseline entry for this field
            base = label_data.get(wkey, {})
            gem_label = info["label"]
            base_label = base.get("label")
            base_src = base.get("source")
            # Weak-override guard: Gemini sometimes returns a lone lowercase
            # fragment lifted from a NEIGHBORING checkbox option that its
            # overlay box happens to overlap — e.g. the "Other (specify)" box
            # sitting one row above a "Primary Diagnosis Code" input makes
            # Gemini answer "specify". When the AcroForm's own /TU tooltip (or
            # a solid multi-word geometry caption) carries the real label and
            # the fragment shares NO tokens with it, keep the baseline: /TU is
            # authored ground truth and a single unrelated lowercase word is
            # almost always an option bleed, not a caption.
            _gem_tokens = set(re.findall(r"[a-z0-9]+", gem_label.lower()))
            _base_tokens = set(re.findall(r"[a-z0-9]+", (base_label or "").lower()))
            _weak_fragment = (
                bool(base_label)
                and base_src not in (None, "name")
                and " " not in gem_label.strip()
                and gem_label.strip()[:1].islower()
                and not (_gem_tokens & _base_tokens)
                and (base_src == "acroform-tu" or len(_base_tokens) >= 2)
            )
            # Short-fragment guard: if Gemini returned a clipped caption (the
            # widget box overlapped the printed label) and geometry/`/TU` found
            # a REAL label, keep the geometry one — it's the whole word.
            if (
                _looks_truncated(gem_label)
                and base_label
                and base_src not in (None, "name")
            ):
                label, source = base_label, base_src
            elif _weak_fragment:
                label, source = base_label, base_src
            else:
                label, source = gem_label, "gemini"
            label_data[wkey] = {
                "label": label,
                "source": source,
                # Keep the pre-Gemini geometry label as a second independent
                # signal for confidence scoring (AI ⇄ geometry agreement).
                "base_label": base.get("label"),
                # Prefer Gemini's section/group; fall back to whatever geometry
                # found so partially-answered fields still get context.
                "section": info.get("section") or base.get("section"),
                "subsection": info.get("subsection") or base.get("subsection"),
                "group": info.get("group") or base.get("group"),
                # Geometry OWNS "is this a bordered grid" (it rarely hallucinates
                # tables); Gemini only names one geometry missed. Gemini supplies
                # the per-cell column header and any conditional-logic hint.
                "table": base.get("table") or info.get("table"),
                "column": info.get("column"),
                "conditional": info.get("conditional"),
                # Structured branch/skip instruction printed for this field
                # ("No, skip to #9", "If Yes, no further questions",
                # "If ≤ -2.5, stop") so downstream logic/UI can honor it.
                "skip_logic": info.get("skip_logic"),
            }

        # Normalize, reconcile radio groups, and carry sections across page
        # breaks. Idempotent — also re-run on cache hits so post-processing
        # improvements reach already-cached templates without a new AI call.
        self._postprocess_label_data(fields_info, label_data)

        # Only cache when Gemini actually contributed — otherwise a transient
        # API failure would freeze the geometry-only labels forever.
        if vision:
            self._label_cache.save(fp, label_data)
        return fields_info, label_data

    def _postprocess_label_data(
        self, fields_info: list[dict], label_data: dict[str, dict]
    ) -> None:
        """Canonicalize/reconcile a label map in place. Kept idempotent so it
        can run both after a fresh Gemini pass AND on a cache hit (post-
        processing improvements then reach old cached templates for free)."""
        checkbox_keys = {
            self._widget_key(f) for f in fields_info if "/Btn" in f.get("type", "")
        }

        # Final normalization sweep across EVERY entry (Gemini-labelled and the
        # geometry baseline kept for missed fields):
        #  - fold dash glyphs on text fields so one title reads as one string
        #    (a table read as "SECTION V ― …" must match its section
        #    "SECTION V — …")
        #  - null synthetic geometry group ids ("group_3")
        #  - ENFORCE group only on checkbox/radio widgets — a text field must
        #    never carry a group (Gemini sometimes leaks the table/question
        #    heading onto plain text cells).
        for wkey, entry in label_data.items():
            for key in ("section", "subsection", "table", "label", "column", "conditional", "skip_logic"):
                if entry.get(key):
                    entry[key] = _norm_dashes(entry[key])
            # Canonicalize the section text so the SAME section reads identically
            # for every field (collapse newlines, drop a prepended instruction
            # line) — otherwise "SECTION VI SERVICES…" and "SECTION VI — …" split.
            if entry.get("section"):
                entry["section"] = _norm_section(entry["section"])
            g = entry.get("group")
            if g:
                g = _norm_dashes(g)
                if re.fullmatch(r"group[_ ]?\d+", str(g), flags=re.IGNORECASE):
                    g = None
                if wkey not in checkbox_keys:
                    g = None
                entry["group"] = g

        # ── Radio-group reconciliation ───────────────────────────────────
        # Every option WIDGET of one radio field is labeled independently by
        # Gemini (each has its own numbered box), so the SAME field can come
        # back with inconsistent group names ("Sex" for Male/Female, "Gender"
        # for Other/Unknown) and mismatched labels (an "Other" box misread as
        # "Male"). Reconcile per underlying field name:
        #   (a) collapse to ONE group — the most common non-empty group across
        #       the field's option widgets (ties: first seen);
        #   (b) when an option's export value is a real word ("Male", "Other",
        #       "Unknown"), use it as the label — the export is the canonical
        #       option identity and immune to Gemini's per-box visual misreads.
        radio_widgets: dict[str, list[dict]] = defaultdict(list)
        for f in fields_info:
            if f.get("_radio_group") and f.get("export_value"):
                radio_widgets[f["name"]].append(f)
        for fname, widgets in radio_widgets.items():
            entries = [
                label_data[self._widget_key(f)]
                for f in widgets
                if self._widget_key(f) in label_data
            ]
            if len(entries) < 2:
                continue
            group_votes = [e.get("group") for e in entries if e.get("group")]
            canonical_group = None
            if group_votes:
                canonical_group = Counter(group_votes).most_common(1)[0][0]
            # A per-box misread shows up as DUPLICATE labels across options that
            # must be distinct (Gemini stamped "Male" on both the Male and the
            # Other box). Each option of a radio field should have a unique
            # label, so any label shared by 2+ options is suspect — fall back to
            # its own export value there. Unique labels (even terse/abbreviated
            # ones like "Continuation of therapy" vs export "Cont") are kept.
            label_counts = Counter(
                (e.get("label") or "").strip().lower()
                for e in entries
                if (e.get("label") or "").strip()
            )
            for f in widgets:
                wkey = self._widget_key(f)
                entry = label_data.get(wkey)
                if not entry:
                    continue
                if canonical_group:
                    entry["group"] = canonical_group
                exp = f.get("export_value")
                cur_lbl = (entry.get("label") or "").strip().lower()
                if (
                    _is_meaningful_export(exp)
                    and (not cur_lbl or label_counts[cur_lbl] > 1)
                ):
                    entry["label"] = exp.strip()
                    if entry.get("source") == "gemini":
                        entry["source"] = "export"

        # ── Section carry-across-page-breaks ─────────────────────────────
        # A section header prints once (e.g. "Section A") and its questions run
        # onto the next page. Gemini only sees that header on page 1, so page-2
        # fields of the same section come back section-less. Walk fields in
        # reading order (page, then top-to-bottom) and forward-fill the last
        # non-empty section so a section is continuous until the NEXT header
        # replaces it. Header/patient fields above the first section stay null.
        ordered = sorted(
            fields_info,
            key=lambda f: (f.get("page", 0), f.get("y", 0), f.get("x", 0)),
        )
        last_section = None
        for f in ordered:
            entry = label_data.get(self._widget_key(f))
            if not entry:
                continue
            sec = entry.get("section")
            if sec:
                last_section = sec
            elif last_section:
                entry["section"] = last_section

    def _label_fields_with_vision_full(
        self,
        pdf_path: str,
        fields_info: list[dict],
    ) -> dict[str, dict]:
        """
        Send the ENTIRE page image + every field on it to Gemini and read back
        a label / section / group for each field.

        Fields are addressed by stable synthetic ids ("f0","f1"…) mapped to
        their ``_widget_key``.  Returns ``dict[widget_key -> {label, section,
        group}]`` for fields Gemini answered; missing fields / failures simply
        aren't present (caller keeps the geometry baseline).
        """
        if not self.api_key or self.api_key in ("-", ""):
            return {}

        try:
            import base64
            import io as _io
            import fitz  # PyMuPDF
        except Exception as e:
            print(f"  ⚠️  Full Gemini vision pass unavailable: {e}")
            return {}

        # Pillow lets us draw a numbered red box on top of each field before
        # sending the image. Without it the model has to map raw pixel coords to
        # the page by itself and drifts (off-by-one caption assignment); the
        # overlay anchors every id to its exact box. Degrade gracefully to the
        # plain page image if Pillow is missing.
        try:
            from PIL import Image, ImageDraw, ImageFont
            _draw_ok = True
        except Exception:
            _draw_ok = False

        _font = None
        if _draw_ok:
            for _fname in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "Arial.ttf"):
                try:
                    _font = ImageFont.truetype(_fname, 13)
                    break
                except Exception:
                    continue
            if _font is None:
                try:
                    _font = ImageFont.load_default()
                except Exception:
                    _font = None

        DPI = 150
        scale = DPI / 72.0
        # Chunk fields per call. Sending ~55 fields at once makes the model
        # return all-empty on dense pages, so we batch to keep it focused.
        CHUNK = 10

        id_to_key: dict[str, str] = {}
        by_page: dict[int, list[tuple[str, dict]]] = {}
        n = 0
        for f in fields_info:
            wkey = self._widget_key(f)
            fid = f"f{n}"
            n += 1
            id_to_key[fid] = wkey
            by_page.setdefault(f["page"], []).append((fid, f))
        if not id_to_key:
            return {}

        resolved: dict[str, dict] = {}
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            print(f"  ⚠️  Full Gemini vision pass: cannot open PDF ({e})")
            return {}

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        # Cache the raw page render per page so retries don't re-rasterize.
        _base_png: dict[int, bytes] = {}
        _base_pil: dict[int, object] = {}

        def _page_render(pno: int):
            if pno not in _base_png:
                pix = doc[pno].get_pixmap(dpi=DPI)
                _base_png[pno] = pix.tobytes("png")
                if _draw_ok:
                    try:
                        _base_pil[pno] = Image.open(
                            _io.BytesIO(_base_png[pno])
                        ).convert("RGB")
                    except Exception:
                        _base_pil[pno] = None
            return _base_png[pno], _base_pil.get(pno)

        def _chunk_data_uri(pno: int, chunk: list) -> str:
            raw_png, base_pil = _page_render(pno)
            if _draw_ok and base_pil is not None:
                im = base_pil.copy()
                d = ImageDraw.Draw(im)
                for fid, f in chunk:
                    x0 = f["x0"] * scale
                    y0 = f["y"] * scale
                    x1 = f["x1"] * scale
                    y1 = f.get("y_bottom", f["y"]) * scale
                    if x1 - x0 < 3:
                        x1 = x0 + 3
                    if y1 - y0 < 3:
                        y1 = y0 + 3
                    # Inset the outline slightly so a widget box that overlaps
                    # its printed caption doesn't have the red line sitting on
                    # top of the label text (which made Gemini read fragments
                    # like "cialty:" instead of "Specialty:").
                    ix0, iy0, ix1, iy1 = x0, y0, x1, y1
                    if x1 - x0 > 6:
                        ix0, ix1 = x0 + 2, x1 - 2
                    if y1 - y0 > 6:
                        iy0, iy1 = y0 + 2, y1 - 2
                    d.rectangle([ix0, iy0, ix1, iy1], outline=(230, 0, 0), width=2)
                    ty = max(0.0, y0 - 15)
                    d.rectangle(
                        [x0, ty, x0 + 8 * len(fid) + 6, ty + 14],
                        fill=(255, 235, 0),
                    )
                    d.text((x0 + 3, ty), fid, fill=(200, 0, 0), font=_font)
                buf = _io.BytesIO()
                im.save(buf, format="PNG")
                raw_png = buf.getvalue()
            b64 = base64.standard_b64encode(raw_png).decode()
            return f"data:image/png;base64,{b64}"

        def _send_chunk(pno: int, chunk: list) -> None:
            data_uri = _chunk_data_uri(pno, chunk)
            fields_text = "\n".join(
                f"- {fid} : [{round(f['x0']*scale,1)}, "
                f"{round(f['y']*scale,1)}, "
                f"{round(f['x1']*scale,1)}, "
                f"{round(f.get('y_bottom', f['y'])*scale,1)}] "
                f"({'checkbox' if '/Btn' in f.get('type','') else 'text'})"
                for fid, f in chunk
            )
            prompt = (
                "You are reading one page of a PDF form. The attached image is "
                "the full page. The fields you must label are OUTLINED IN RED, "
                "and each red box has its id (e.g. f3) printed in a yellow tag "
                "at its top-left corner. Match each id to its red box on the "
                "image, then read the printed text next to THAT box.\n\n"
                "Below is the same subset of fields with id, pixel bounding box "
                "[x0,y0,x1,y1] and kind (text or checkbox) as a cross-check.\n\n"
                "For EACH listed field return an OBJECT with these STRING "
                "values:\n"
                "  - \"label\": for a TEXT field, the printed prompt/caption "
                "describing what goes there; for a CHECKBOX, the specific option "
                "text printed beside that box (e.g. 'Urgent', 'Male') — NOT the "
                "group heading.\n"
                "  - \"section\": the EXACT printed section header this field "
                "sits under, copied verbatim INCLUDING its number (e.g. "
                "'SECTION IV — PATIENT INFORMATION'). Use the identical string "
                "for every field in that section. \"\" if none.\n"
                "  - \"subsection\": a sub-heading or printed INSTRUCTION inside "
                "the section that scopes this field — a sub-block title "
                "('Compound Drug Information') or a parenthetical instruction "
                "('If this is a compound drug, complete this part'). Copy it "
                "verbatim. \"\" if the field has no such sub-heading.\n"
                "  - \"group\": ONLY for a CHECKBOX that is one option among "
                "several — the shared question/heading printed on the form "
                "(e.g. 'Review Type', 'Sex'). For ANY text field this MUST be "
                "\"\". Use the ACTUAL printed heading — never invent "
                "placeholders.\n"
                "  - \"table\": if the field sits inside a bordered grid of rows "
                "AND columns, the table's title/header (e.g. 'Medication "
                "History'); else \"\".\n"
                "  - \"column\": if the field is a cell under a column header in "
                "that table, the column header text (e.g. 'Drug Name', "
                "'Strength', 'NDC #'); else \"\".\n"
                "  - \"conditional\": if this field only applies when another "
                "option is selected, state it briefly (e.g. \"Only if 'Other' "
                "is checked\", \"Only if Continuation of therapy\"); else "
                "\"\".\n"
                "  - \"skip_logic\": any printed BRANCH/SKIP/STOP instruction "
                "attached to this field or option, copied verbatim (e.g. "
                "\"No, skip to #9\", \"If Yes, no further questions\", "
                "\"If ≤ -2.5, stop\"); else \"\".\n\n"
                "Every value MUST be a string (use \"\" when not applicable); "
                "never use true/false/null.\n"
                "Respond ONLY with strict JSON mapping each id to its object, "
                "e.g.:\n"
                "{\"f0\": {\"label\": \"Patient Name\", \"section\": "
                "\"SECTION IV — PATIENT INFORMATION\", \"subsection\": \"\", "
                "\"group\": \"\", "
                "\"table\": \"\", \"column\": \"\", \"conditional\": \"\", "
                "\"skip_logic\": \"\"}, "
                "\"f1\": {\"label\": \"No\", \"section\": \"SECTION II — "
                "REVIEW\", \"subsection\": \"\", \"group\": \"Review Type\", "
                "\"table\": \"\", "
                "\"column\": \"\", \"conditional\": \"\", "
                "\"skip_logic\": \"No, skip to #9\"}}\n"
                "Read text verbatim.\n\n"
                f"Fields:\n{fields_text}"
            )
            resp = client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                max_tokens=2600,
                messages=[
                    {"role": "system", "content": "Return strict JSON only. No markdown."},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": prompt},
                    ]},
                ],
            )
            raw = (resp.choices[0].message.content or "").strip()
            self._merge_vision_chunk(raw, id_to_key, resolved)

        try:
            # First pass — chunk every page.
            for pno, page_fields in by_page.items():
                if pno >= doc.page_count:
                    continue
                for i in range(0, len(page_fields), CHUNK):
                    try:
                        _send_chunk(pno, page_fields[i:i + CHUNK])
                    except Exception as e:
                        print(f"  ⚠️  Gemini chunk failed (p{pno} @{i}): {e}")

            # Retry pass — any id still unresolved (empty/failed chunk) gets one
            # more try in SMALLER batches, which the model answers more reliably.
            missing_by_page: dict[int, list] = {}
            for pno, page_fields in by_page.items():
                for fid, f in page_fields:
                    if id_to_key[fid] not in resolved:
                        missing_by_page.setdefault(pno, []).append((fid, f))
            for pno, mf in missing_by_page.items():
                if pno >= doc.page_count:
                    continue
                for i in range(0, len(mf), 5):
                    try:
                        _send_chunk(pno, mf[i:i + 5])
                    except Exception as e:
                        print(f"  ⚠️  Gemini retry chunk failed (p{pno} @{i}): {e}")

            return resolved
        finally:
            doc.close()

    def _merge_vision_chunk(
        self, raw: str, id_to_key: dict[str, str], resolved: dict[str, dict]
    ) -> None:
        """Parse one Gemini chunk response and merge into ``resolved``."""
        import json as _json
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        try:
            parsed = _json.loads(raw)
        except Exception:
            return
        if not isinstance(parsed, dict):
            return
        for fid, info in parsed.items():
            if fid not in id_to_key:
                continue
            # Tolerate both shapes: a full object OR a bare label string
            # (some responses collapse to that).
            if isinstance(info, dict):
                lbl = str(info.get("label", "") or "").strip()
                sec = str(info.get("section", "") or "").strip()
                sub = str(info.get("subsection", "") or "").strip()
                grp = str(info.get("group", "") or "").strip()
                tbl = str(info.get("table", "") or "").strip()
                col = str(info.get("column", "") or "").strip()
                cond = str(info.get("conditional", "") or "").strip()
                skip = str(info.get("skip_logic", "") or "").strip()
            elif isinstance(info, str):
                lbl, sec, sub, grp, tbl, col, cond, skip = info.strip(), "", "", "", "", "", "", ""
            else:
                continue
            # Fold dash variants so a section title read from two chunks with
            # different dash glyphs doesn't split into two "sections".
            sec = _norm_dashes(sec)
            sub = _norm_dashes(sub)
            grp = _norm_dashes(grp)
            tbl = _norm_dashes(tbl)
            col = _norm_dashes(col)
            # Drop invented placeholder ids (e.g. "group_1", "table_2").
            if re.fullmatch(r"group[_ ]?\d+", grp, flags=re.IGNORECASE):
                grp = ""
            if re.fullmatch(r"table[_ ]?\d+", tbl, flags=re.IGNORECASE):
                tbl = ""
            out: dict = {}
            if lbl:
                out["label"] = lbl
            if sec:
                out["section"] = sec
            if sub:
                out["subsection"] = sub
            if grp:
                out["group"] = grp
            if tbl:
                out["table"] = tbl
            if col:
                out["column"] = col
            if cond:
                out["conditional"] = cond
            if skip:
                out["skip_logic"] = _norm_dashes(skip)
            if out:
                resolved[id_to_key[fid]] = out

    def _inspect_opencv(
        self, fillable_pdf_path: str
    ) -> tuple[list[dict], dict]:
        """
        OpenCV detection (local, no AI).  Boxes bind to AcroForm widgets so
        fills keep working; labels reuse pdfplumber geometry, with OCR only
        when a page has no text layer.  Falls back to AcroForm if OpenCV finds
        nothing or its dependencies are missing.
        """
        acro_fields = self._get_fields_with_coords(fillable_pdf_path)
        cv = None
        fields_info: list[dict] = []
        try:
            from .cv_field_service import CVFieldService, CVDependencyError
            cv = CVFieldService(
                dpi=settings.CV_DPI,
                ocr_enabled=settings.CV_OCR_ENABLED,
                match_iou=settings.CV_MATCH_IOU,
            )
            fields_info = cv.detect_fields(
                fillable_pdf_path, acroform_fields=acro_fields or None
            )
        except CVDependencyError as exc:
            print(f"  opencv engine unavailable ({exc}); falling back to acroform")
        except Exception as exc:
            print(f"  opencv detection failed ({exc}); falling back to acroform")

        if not fields_info:
            # Pure AcroForm fallback (CV found nothing): these are real widgets
            # read straight from the PDF, so their /TU is trustworthy — keep it.
            fields_info = acro_fields
        else:
            # CV detected/re-bound boxes. Their geometry comes from vision and
            # is IoU-matched to widgets imperfectly, so a box's /TU no longer
            # provably belongs to it (and reused names like "0"/"1"/"2" make it
            # worse). Drop the /TU hints here so this engine behaves exactly as
            # it did before /TU support — label purely from box geometry/OCR.
            for _f in fields_info:
                _f.pop("_acro", None)
                _f.pop("tu", None)
        if not fields_info:
            return [], {}

        label_data = self._extract_labels_for_fields(fillable_pdf_path, fields_info)

        # OCR fallback for fields still unlabeled (scanned pages: no text layer)
        if cv is not None and settings.CV_OCR_ENABLED:
            unlabeled = [
                f for f in fields_info
                if label_data.get(self._widget_key(f), {}).get("source", "name") == "name"
            ]
            if unlabeled:
                try:
                    ocr = cv.ocr_labels(fillable_pdf_path, unlabeled)
                    for name, lbl in ocr.items():
                        if lbl:
                            label_data[name] = {
                                "label": lbl, "source": "ocr",
                                "section": None, "group": None,
                            }
                except Exception as exc:
                    print(f"  cv-ocr labeling failed ({exc})")
        return fields_info, label_data

    def _inspect_vlm_local(
        self, fillable_pdf_path: str
    ) -> tuple[list[dict], dict]:
        """
        Local Qwen-VL understanding.  Detects via AcroForm (or OpenCV when the
        PDF has no widgets), runs the geometry baseline, then lets the VLM
        override label/section/group.  Falls back to geometry on any failure.
        """
        fields_info = self._get_fields_with_coords(fillable_pdf_path)
        if not fields_info:
            try:
                from .cv_field_service import CVFieldService
                cv = CVFieldService(dpi=settings.CV_DPI, match_iou=settings.CV_MATCH_IOU)
                fields_info = cv.detect_fields(fillable_pdf_path)
            except Exception as exc:
                print(f"  vlm_local: OpenCV detect failed ({exc})")
                fields_info = []
        if not fields_info:
            return [], {}

        label_data = self._extract_labels_for_fields(fillable_pdf_path, fields_info)

        try:
            from .vlm_field_service import VLMFieldService
            vlm = VLMFieldService(
                self.api_key, self.base_url, self.model, dpi=settings.CV_DPI
            )
            vlm_data = vlm.understand_fields(fillable_pdf_path, fields_info)
            # vlm_data is keyed by field NAME, but label_data (and the row
            # builder) key by _widget_key, which for real widgets is now
            # name+/TU. Route each VLM label onto every widget key sharing that
            # name so the override actually lands (otherwise the lookup misses
            # and the field silently falls back / shows blank).
            name_to_keys: dict[str, list[str]] = {}
            for f in fields_info:
                name_to_keys.setdefault(f["name"], []).append(self._widget_key(f))
            for name, d in vlm_data.items():
                if d.get("label"):
                    for wk in name_to_keys.get(name, [name]):
                        label_data[wk] = d
        except Exception as exc:
            print(f"  vlm_local understanding failed ({exc}); using geometry")
        return fields_info, label_data

    def _build_inspect_rows(
        self, fields_info: list[dict], label_data: dict
    ) -> dict:
        """Shared row builder for all engines."""
        from .form_spec_builder import link_inline_blanks

        _links = link_inline_blanks(fields_info)

        def _linked_checkbox(tf: dict) -> Optional[str]:
            return _links.get(tf["name"])

        # Duplicate-label detection (a confidence signal): the same label under
        # the same section on 2+ NON-radio fields usually means a mis-mapping
        # (repeated caption grabbed for several boxes). Radio options legitimately
        # repeat, so they're excluded.
        _label_sec_counts: dict[tuple, int] = defaultdict(int)
        for f in fields_info:
            if f.get("_radio_group"):
                continue
            e = label_data.get(self._widget_key(f), {})
            lab = (e.get("label") or "").strip().lower()
            if lab:
                _label_sec_counts[(lab, (e.get("section") or ""))] += 1

        rows = []
        for f in fields_info:
            ft = f["type"]
            if "/Tx" in ft:
                field_kind = "text"
            elif "/Btn" in ft:
                field_kind = "checkbox"
            elif "/Sig" in ft:
                field_kind = "signature"
            else:
                field_kind = "other"
            linked_field = _linked_checkbox(f) if field_kind == "text" else None
            entry = label_data.get(
                self._widget_key(f),
                {"label": _humanize_field_name(f["name"]), "source": "name", "section": None, "group": None},
            )
            # Geometry disambiguation sometimes prefixes the section onto a
            # checkbox OPTION when it has no row context ("Section A: … - No").
            # For an option that prefix is noise — strip it back to the choice.
            _final_label = entry.get("label")
            _sec = entry.get("section")
            if field_kind == "checkbox" and _final_label and _sec:
                _pref = f"{_sec} - "
                if _final_label.startswith(_pref):
                    _stripped = _final_label[len(_pref):].strip()
                    if _stripped:
                        _final_label = _stripped
            _lab = (_final_label or "").strip().lower()
            _is_dup = (
                not f.get("_radio_group")
                and bool(_lab)
                and _label_sec_counts.get((_lab, (entry.get("section") or "")), 0) > 1
            )
            confidence = _compute_confidence(
                label=_final_label,
                source=entry.get("source"),
                tu=f.get("tu"),
                section=entry.get("section"),
                base_label=entry.get("base_label"),
                is_dup=_is_dup,
            )
            rows.append(
                {
                    "name": f["name"],
                    "qualified_name": f.get("qualified_name"),
                    "field_type": field_kind,
                    "page": f["page"],
                    "label": _final_label,
                    "label_source": entry["source"],
                    "section": entry.get("section"),
                    "subsection": entry.get("subsection"),
                    "group": entry.get("group"),
                    "table": entry.get("table"),
                    "column": entry.get("column"),
                    "conditional": entry.get("conditional"),
                    "skip_logic": entry.get("skip_logic"),
                    "confidence": confidence,
                    # Per-option export value (e.g. "Male"/"Female", "Yes"/"No")
                    # so the extractor can surface EACH option of a radio group
                    # as its own row instead of collapsing to one field.
                    "export_value": f.get("export_value"),
                    "radio_option": bool(f.get("_radio_group")),
                    "linked_field": linked_field,
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
        Map user_data to PDF field values via a PHI-FREE two-phase design.

        Phase 1 — SCHEMA MAPPING (cached / AI): decide which USER KEY feeds
        which PDF field → ``{pdf_field: {source_key, confidence}}``. This depends
        only on labels + key NAMES, never on values, so:
          * PHI values are never sent to the AI (on a hit OR a miss), and
          * only schema (field names, labels, key names) is written to the cache.
        Phase 2 — LOCAL APPLY: copy each user value into its mapped field here in
        Python. Checkbox/radio on-state resolution happens later in the writer.

        Returns:
            values      - {field_name: value_to_fill}
            confidence  - {field_name: 0.0-1.0}
            cache_hit   - True if the mapping came from the field-map cache
        """
        # ── Detect canonical bundle vs plain flat dict ────────────────────────
        if "flat" in user_data and "structured" in user_data:
            flat_data = user_data["flat"]
            structured_data = user_data["structured"]
        else:
            flat_data = user_data
            structured_data = None

        flat_data = {k: v for k, v in (flat_data or {}).items() if isinstance(k, str)}
        user_keys = sorted(flat_data.keys())

        def _apply(mapping: Dict[str, dict]) -> Tuple[Dict[str, str], Dict[str, float]]:
            """Copy values into fields per the schema mapping (no AI, no egress)."""
            out_values: Dict[str, str] = {}
            out_conf: Dict[str, float] = {}
            for field_name, m in mapping.items():
                if not isinstance(m, dict):
                    continue
                src = m.get("source_key")
                if not src or src not in flat_data:
                    continue
                val = flat_data[src]
                if val in (None, ""):
                    continue
                out_values[field_name] = str(val)
                out_conf[field_name] = float(m.get("confidence", 1.0))
            return out_values, out_conf

        # ── Cache lookup (schema only — no values in the key or on disk) ──────
        fp = self._map_cache.fingerprint(field_labels, user_keys, model=self.model or "")
        cached = self._map_cache.get(fp)
        if cached is not None:
            values, confidence = _apply(cached)
            print(f"  \u2705  Field-map cache HIT (fp={fp[:8]}\u2026) \u2014 no AI call, applied locally")
            return values, confidence, True

        # ── Miss: ask the AI for the schema mapping (KEYS only, never values) ─
        labeled_fields = _build_labeled_fields(fields_info, field_labels)

        prompt = (
            "You are a form-filling schema mapper.\n\n"
            "You are given (a) a list of PDF form FIELDS, each with the label\n"
            "printed next to it, and (b) a list of available USER DATA KEYS.\n"
            "You are NOT given the user's values — only the key names.\n\n"
            "Your job: for each PDF field, choose which ONE user data key best\n"
            "feeds it, based on the field label and the key name.\n\n"
            "Rules:\n"
            "  - Return ONLY a JSON object of the form:\n"
            "      {\"<field_name>\": {\"source_key\": \"<user_key>\", \"confidence\": <0.0-1.0>}}\n"
            "  - source_key MUST be one of the provided USER DATA KEYS, copied exactly.\n"
            "  - Omit any field that no key sensibly feeds. Do not invent keys.\n"
            "  - When similar keys exist (e.g. 'physician_phone' vs 'patient_phone'),\n"
            "    use the label context to disambiguate.\n"
            "  - confidence 1.0 = certain, 0.5 = plausible, 0.0 = guessing.\n"
            "  - No markdown, no code fences, no explanation.\n\n"
            f"PDF FORM FIELDS WITH LABELS:\n{json.dumps(labeled_fields, indent=2)}\n\n"
            f"AVAILABLE USER DATA KEYS:\n{json.dumps(user_keys, indent=2)}\n"
        )
        if structured_data:
            # Send only the STRUCTURE (key paths), never the values.
            structured_keys = _structured_key_paths(structured_data)
            if structured_keys:
                prompt += (
                    f"\nSTRUCTURED KEY PATHS (for disambiguation, no values):\n"
                    f"{json.dumps(structured_keys, indent=2)}\n"
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

        # Keep only mappings whose source_key actually exists in the user data.
        valid_keys = set(flat_data.keys())
        mapping: Dict[str, dict] = {}
        for field_name, v in parsed.items():
            if not isinstance(field_name, str):
                continue
            if isinstance(v, dict):
                src = v.get("source_key")
                conf = float(v.get("confidence", 1.0))
            else:
                src = str(v)
                conf = 1.0
            if isinstance(src, str) and src in valid_keys:
                mapping[field_name] = {"source_key": src, "confidence": conf}

        if mapping:
            self._map_cache.set(
                fp, mapping, field_labels=field_labels, user_keys=user_keys
            )
            print(f"  \U0001f4be  Field-map cache STORED (fp={fp[:8]}\u2026, {len(mapping)} fields, no PHI)")

        values, confidence = _apply(mapping)
        return values, confidence, False

    # ------------------------------------------------------------------
    # Step 3a: Canonical fork ("Call 4") — canonical-first, PHI-free fill
    # ------------------------------------------------------------------

    def _canonical_fork(
        self,
        fields_info: list[dict],
        field_labels: dict[str, str],
        user_data: dict,
    ) -> Tuple[Dict[str, str], Dict[str, float], dict]:
        """
        Fill fields via the FIXED canonical schema before the general mapper.

        Two PHI-free schema joins meet at the canonical hub:
          * form side  — field → canonical path  (cached, ``CanonicalFieldService``)
          * data side  — user KEY → canonical path (``resolve_label`` on key names)
        A field is filled only when both sides agree on the same canonical path.

        Critical fields (member_id / dob / npi / drug …) are DEFERRED — left blank
        and reported — unless the mapping confidence clears
        ``CANONICAL_CRITICAL_MIN_CONFIDENCE`` (a wrong critical value denies the PA).

        Returns (values, confidence, report). ``report['deferred']`` lists the
        field names the caller must ALSO keep the general fork away from.
        """
        from ..models.pa_canonical import (
            BY_PATH,
            CRITICAL_FIELDS,
            acro_field_name,
            map_key_export,
            option_values_match,
            resolve_label,
        )
        from .pa_normalize import normalize

        # ── Normalize incoming shape (canonical bundle vs plain flat dict) ────
        if "flat" in user_data and "structured" in user_data:
            flat_data = user_data.get("flat") or {}
        else:
            flat_data = user_data
        flat_data = {k: v for k, v in (flat_data or {}).items() if isinstance(k, str)}

        # ── Data side: user key → canonical path → value (LOCAL only) ─────────
        # User keys are often snake_case ("member_id", "patient_name") while the
        # canonical aliases are space-separated ("member id", "patient name"), so
        # also try a de-underscored/hyphenated variant of each key.
        canonical_values: Dict[str, str] = {}
        for key, val in flat_data.items():
            if val in (None, ""):
                continue
            # Guided-intake / CSV data arrives already keyed by canonical path
            # ("patient.dob"). Honor those verbatim — deterministic, no guessing.
            if key in BY_PATH:
                path = key
            else:
                path = resolve_label(key)
                if not path:
                    spaced = key.replace("_", " ").replace("-", " ")
                    if spaced != key:
                        path = resolve_label(spaced)
            if path and path not in canonical_values:
                # Preserve lists (multi-row table columns); stringify scalars.
                canonical_values[path] = val if isinstance(val, (list, tuple)) else str(val)

        # Checkbox answers and narratives are answered against THIS form's own
        # questions rather than the catalog, so they resolve independently of
        # whether any canonical path matched.
        spec_values, spec_conf = self._form_spec_values(
            fields_info, flat_data, canonical_values
        )

        if not canonical_values:
            return (
                dict(spec_values),
                dict(spec_conf),
                {"filled": len(spec_values), "deferred": [], "mapped_fields": 0,
                 "form_spec_filled": len(spec_values)},
            )

        # ── Form side: field → canonical path (cached, PHI-free) ──────────────
        field_canon = self._canonical_service.map_fields(fields_info, field_labels)

        type_by_name = {f.get("name"): str(f.get("type", "")) for f in fields_info}
        conf_rank = {"high": 0.9, "medium": 0.7, "low": 0.4}
        crit_min = settings.CANONICAL_CRITICAL_MIN_CONFIDENCE

        values: Dict[str, str] = {}
        confidence: Dict[str, float] = {}
        deferred: list[dict] = []

        def _write(nm: str, path: str, value: str, conf: float) -> None:
            """Normalize + record one field value, honoring critical deferral."""
            if value in (None, ""):
                return
            if path in CRITICAL_FIELDS and conf < crit_min:
                deferred.append({"field": nm, "canonical": path, "confidence": conf})
                return
            if "/Btn" in type_by_name.get(nm, ""):
                out = value                          # writer resolves the on-state
            else:
                ftype = BY_PATH[path].type if path in BY_PATH else "text"
                out = normalize(value, ftype) or value
            values[nm] = out
            confidence[nm] = conf

        def _user_matches(path: str, opt: str, raw) -> bool:
            if isinstance(raw, (list, tuple)):
                return any(
                    option_values_match(path, u, opt)
                    for u in raw if u not in (None, "")
                )
            return option_values_match(path, raw, opt)

        # ── Pass 1: option-valued widgets (checkbox/radio → path + value) ─────
        # Only tick when the user's data for that path selects this option.
        # Write the AcroForm export (from name::export) or a generic Yes for
        # independent checkboxes — never the catalog choice code (PT≠/On).
        option_keys: set = set()
        for key, m in field_canon.items():
            if not isinstance(m, dict):
                continue
            path = m.get("canonical")
            opt = m.get("value")
            if not path or path == "other" or opt in (None, ""):
                continue
            if path not in canonical_values:
                continue
            conf = conf_rank.get(str(m.get("confidence", "medium")).lower(), 0.7)
            option_keys.add(key)
            if not _user_matches(path, str(opt), canonical_values[path]):
                continue
            acro = acro_field_name(key)
            write_val = map_key_export(key) or "Yes"
            _write(acro, path, write_val, conf)

        # ── Pass 2: ordinary fields (no option value) — path-grouped write ────
        path_fields: dict = defaultdict(list)
        for key, m in field_canon.items():
            if key in option_keys or not isinstance(m, dict):
                continue
            path = m.get("canonical")
            if not path or path == "other" or path not in canonical_values:
                continue
            conf = conf_rank.get(str(m.get("confidence", "medium")).lower(), 0.7)
            path_fields[path].append((acro_field_name(key), conf))

        for path, nc in path_fields.items():
            raw = canonical_values[path]

            # ── List value → distribute one item per row across the biggest run.
            if isinstance(raw, (list, tuple)):
                items = [str(v) for v in raw if v not in (None, "")]
                run = self.largest_row_run([n for n, _ in nc], fields_info)
                if len(run) >= 2 and items:
                    conf_by_name = dict(nc)
                    for i, nm in enumerate(run):
                        if i >= len(items):
                            break                    # fewer values than rows: rest blank
                        _write(nm, path, items[i], conf_by_name.get(nm, 0.7))
                    continue
                # No multi-row run (or empty list): fall back to the first value.
                raw = items[0] if items else ""

            # ── Scalar value → write to all non-option fields on this path.
            scalar = str(raw) if raw is not None else ""
            for nm, conf in nc:
                _write(nm, path, scalar, conf)

        # Form-specific answers last: a reviewer-approved question is a more
        # direct statement of intent than a catalog inference on the same widget.
        values.update(spec_values)
        confidence.update(spec_conf)

        mapped_fields = sum(
            1 for mm in field_canon.values()
            if isinstance(mm, dict) and mm.get("canonical") not in (None, "other")
        )
        return values, confidence, {
            "filled": len(values),
            "deferred": deferred,
            "mapped_fields": mapped_fields,
            "form_spec_filled": len(spec_values),
        }

    def _form_spec_values(
        self,
        fields_info: list[dict],
        flat_data: Dict[str, object],
        canonical_values: Dict[str, object],
    ) -> tuple[Dict[str, str], Dict[str, float]]:
        """Resolve this form's own questions and narratives into widget values.

        Answers are looked up by question id (``q:<id>``, matching the CSV
        template header), then by the question's printed text. A question
        carrying an opt-in ``canonical_hint`` also accepts the value already
        collected for that catalog path, which is what lets a recurring
        question prefill from a patient profile.
        """
        from ..models.pa_canonical import acro_field_name, map_key_export
        from .form_spec_cache import FormSpecCache

        try:
            spec = FormSpecCache().get(
                self._canonical_service._cache.signature(fields_info)
            )
        except Exception:
            spec = None
        if spec is None:
            return {}, {}

        def answer(*keys) -> object:
            for k in keys:
                if k and k in flat_data and flat_data[k] not in (None, "", []):
                    return flat_data[k]
            return None

        values: Dict[str, str] = {}
        confidence: Dict[str, float] = {}

        for q in spec.questions:
            ans = answer(f"q:{q.id}", q.id, q.question)
            if ans is None and q.canonical_hint:
                ans = canonical_values.get(q.canonical_hint)
            if ans in (None, "", []):
                continue
            chosen = {
                str(a).strip().lower()
                for a in (ans if isinstance(ans, (list, tuple)) else [ans])
                if a not in (None, "")
            }
            for o in q.options:
                aliases = {o.label.strip().lower(), o.field.strip().lower()}
                if o.export:
                    aliases.add(o.export.strip().lower())
                if not (chosen & (aliases - {""})):
                    continue
                acro = acro_field_name(o.field)
                # Write the AcroForm on-state, never the printed option text.
                values[acro] = map_key_export(o.field) or o.export or "Yes"
                confidence[acro] = 0.95

        for lt in spec.long_text:
            ans = answer(f"t:{lt.field}", lt.field, lt.label)
            if ans in (None, "", []):
                continue
            values[acro_field_name(lt.field)] = str(ans)
            confidence[acro_field_name(lt.field)] = 0.95

        # Form-specific tables: each cell keyed like narratives (``t:<acro>``).
        for table in getattr(spec, "tables", None) or []:
            for col in table.columns:
                for field in col.fields:
                    ans = answer(f"t:{field}", field)
                    if ans in (None, "", []):
                        continue
                    values[acro_field_name(field)] = str(ans)
                    confidence[acro_field_name(field)] = 0.95

        # Typed signature lines (+ companion dates) — ``t:<field>`` → AcroForm.
        for s in getattr(spec, "signatures", None) or []:
            ans = answer(f"t:{s.field}", s.field, f"t:{s.acro_field}", s.acro_field)
            if ans in (None, "", []):
                # Optional catalog fallback for text signature blanks.
                if getattr(s, "kind", "signature") == "date":
                    ans = canonical_values.get("request.signature_date")
                else:
                    ans = canonical_values.get("request.signature")
            if ans in (None, "", []):
                continue
            acro = acro_field_name(s.acro_field or s.field)
            if acro:
                values[acro] = str(ans)
                confidence[acro] = 0.95

        # Leftover extras (unmapped / other) — direct AcroForm write via ``t:``.
        _truthy = {"yes", "y", "true", "on", "1", "checked"}
        _falsy = {"", "no", "n", "false", "off", "0", "unchecked"}
        for ex in getattr(spec, "extras", None) or []:
            ans = answer(f"t:{ex.field}", ex.field, f"t:{ex.acro_field}", ex.acro_field)
            if ans in (None, "", []):
                continue
            acro = acro_field_name(ex.acro_field or ex.field)
            if not acro:
                continue
            if ex.kind == "checkbox":
                chosen = {
                    str(a).strip().lower()
                    for a in (ans if isinstance(ans, (list, tuple)) else [ans])
                    if a not in (None, "")
                }
                label_l = (ex.label or "").strip().lower()
                if chosen <= _falsy:
                    continue
                if not (
                    chosen & _truthy
                    or (label_l and label_l in chosen)
                    or bool(chosen - _falsy)
                ):
                    continue
                values[acro] = (
                    map_key_export(ex.field)
                    or ex.export
                    or "Yes"
                )
            else:
                values[acro] = str(ans)
            confidence[acro] = 0.95

        return values, confidence

    # ------------------------------------------------------------------
    # Repeating-column detection (shared: fork distribution + schema hints)
    # ------------------------------------------------------------------

    @staticmethod
    def largest_row_run(names: list[str], fields_info: list[dict]) -> list[str]:
        """Ordered (top→bottom) names of the biggest contiguous table-row run.

        A repeating column's widgets share a page + x-band and sit in a tight
        vertical pitch; widgets of the same canonical path that live in other
        sections form separate runs. Returns the longest such run so a list of
        values can be distributed one per row (and so the schema can flag a field
        as multi-row).
        """
        X_TOL = 12.0
        ROW_GAP_MAX = 45.0
        coord = {
            f.get("name"): (
                f.get("page", 0),
                float(f.get("x0", 0) or 0),
                float(f.get("y", 0) or 0),
            )
            for f in fields_info
        }
        cols: dict = defaultdict(list)
        for n in names:
            pg, x, y = coord.get(n, (0, 0.0, 0.0))
            cols[(pg, round(x / X_TOL))].append((y, n))
        best: list[str] = []
        for members in cols.values():
            members.sort()
            run: list[str] = []
            prev = None
            for y, n in members:
                if prev is not None and (y - prev) > ROW_GAP_MAX:
                    if len(run) > len(best):
                        best = run[:]
                    run = []
                run.append(n)
                prev = y
            if len(run) > len(best):
                best = run[:]
        return best

    # ------------------------------------------------------------------
    # Step 3c: Suppress single-value broadcast across repeating table rows
    # ------------------------------------------------------------------

    def _suppress_row_broadcast(
        self,
        field_values: Dict[str, str],
        confidence: Dict[str, float],
        fields_info: list[dict],
        field_labels: Dict[str, str],
    ) -> Tuple[Dict[str, str], Dict[str, float], list[str]]:
        """Stop one scalar value from being copied into every row of a table.

        A "repeating column" is 2+ text fields that share the same page, the
        same printed label, the same x-position (one table column), AND sit in
        a tight vertical run (consecutive row pitch). A flat, single-valued
        input has ONE value to give — not one per row — so when such rows would
        all receive the IDENTICAL value we keep it on the first (topmost) row
        and blank the rest.

        Fields that merely share a label across different form sections (a large
        vertical gap, e.g. patient "Name" vs prescriber "Name") are NOT a table
        run and are left untouched. Distinct per-row values are also kept.

        Returns (values, confidence, dropped_field_names).
        """
        X_TOL = 12.0        # x0 within this band => same table column
        ROW_GAP_MAX = 45.0  # vertical gap above this => different table/section

        by_name = {f.get("name"): f for f in fields_info}
        groups: dict[tuple, list[tuple[float, str]]] = defaultdict(list)
        for name in field_values:
            f = by_name.get(name)
            if not f:
                continue
            # Buttons/checkboxes are option widgets, not repeating text cells.
            if "/Btn" in f.get("type", ""):
                continue
            label = (field_labels.get(name) or "").strip().lower()
            if not label:
                continue
            page = f.get("page", 0)
            xband = round(float(f.get("x0", 0) or 0) / X_TOL)
            groups[(page, xband, label)].append((float(f.get("y", 0) or 0), name))

        dropped: list[str] = []
        for members in groups.values():
            if len(members) < 2:
                continue
            members.sort()  # top -> bottom by y
            # Split the column into runs; a big vertical gap ends a run.
            run: list[str] = []
            prev_y: Optional[float] = None
            runs: list[list[str]] = []
            for y, nm in members:
                if prev_y is not None and (y - prev_y) > ROW_GAP_MAX:
                    runs.append(run)
                    run = []
                run.append(nm)
                prev_y = y
            if run:
                runs.append(run)
            # Within each contiguous row-run, keep the first field per distinct
            # value and blank later rows carrying an IDENTICAL value.
            for r in runs:
                if len(r) < 2:
                    continue
                seen_values: set = set()
                for nm in r:  # already top -> bottom
                    val = field_values.get(nm)
                    if val in (None, ""):
                        continue
                    if val in seen_values:
                        dropped.append(nm)
                    else:
                        seen_values.add(val)

        if dropped:
            for nm in dropped:
                field_values.pop(nm, None)
                confidence.pop(nm, None)
        return field_values, confidence, dropped

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
            from .pdf_service import PDFService

            reader = PdfReader(input_path)

            # Split values into BUTTONS (checkbox/radio) and TEXT — they need
            # different write strategies. Buttons must resolve to a real PDF
            # on-state (e.g. "/Female", "/Initial Request") and have /AS set on
            # the correct kid widget; the old code wrote the raw value as text
            # onto every button, producing garbage like V='/' and unticked boxes.
            btn_states = PDFService._collect_button_states(reader)
            button_norm: Dict[str, str] = {}
            text_vals: Dict[str, str] = {}
            for k, v in field_values.items():
                if v in (None, ""):
                    continue
                if k in btn_states:
                    state = PDFService._resolve_button_state(v, btn_states[k])
                    if state and state != "/Off":
                        button_norm[k] = state
                else:
                    text_vals[k] = v

            writer = PdfWriter()
            writer.append(reader)

            filled = 0
            # Buttons: let pypdf set /V + /AS on the matching widgets (radio-
            # aware). Safe on commonforms PDFs — button appearance streams
            # (/AP /N) already exist, so no font resources are needed.
            if button_norm:
                for page in writer.pages:
                    try:
                        writer.update_page_form_field_values(page, button_norm)
                    except Exception:
                        pass
                filled += len(button_norm)

            # Text: set /V at annotation level and drop /AP so the viewer
            # regenerates the appearance. This path tolerates the missing-font
            # resources that crash update_page_form_field_values on
            # commonforms-generated fields (the reason this writer exists).
            if text_vals:
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
                            if field_name in text_vals and text_vals[field_name]:
                                annot[NameObject("/V")] = TextStringObject(
                                    text_vals[field_name]
                                )
                                if "/AP" in annot:
                                    del annot["/AP"]
                                filled += 1
                        except Exception:
                            continue

            print(
                f"  ✏️  Fields written: {filled}/{len(field_values)} "
                f"(btn={len(button_norm)}, tx={len(text_vals)})"
            )

            with open(output_path, "wb") as fh:
                writer.write(fh)
            return True
        except Exception as e:
            print(f"  ❌ Error writing filled PDF: {e}")
            return False

    # ------------------------------------------------------------------
    # Label flattening (shared by autofill + the mapping-review draft builder)
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_field_labels(
        fields_info: list[dict], label_data: dict[str, dict]
    ) -> Dict[str, str]:
        """Flatten per-field label context to one string per field.

        When section/group context is known it is prepended as
        "Section / Group / Label" so duplicate labels across sections
        ("Name" x3) or checkbox option runs under one group question become
        distinguishable. AcroForm /TU tooltips are used as an authoritative
        fallback when geometry produced no label, then the raw field name.
        Iterates ALL fields so /TU-only fields still get labeled.

        ``label_data`` is keyed by :meth:`_widget_key` (plain name, or
        ``name\\x1fexport`` / ``name\\x1fTU`` for radio options and
        /TU-scoped widgets). Lookup must use that same key — looking up by
        plain name silently misses option labels (Male/Female → ``undefined_3``).

        Output keys use :func:`map_field_key` so radio-group options become
        ``name::export`` (one row per option, same as extract) while ordinary
        widgets stay under their AcroForm name. Fill strips ``::export`` when
        writing.
        """
        from ..models.pa_canonical import map_field_key

        tu_by_name = {f["name"]: (f.get("tu") or "").strip() for f in fields_info}

        def _ai_label(f: dict) -> str:
            name = f["name"]
            wkey = VisionService._widget_key(f)
            # Prefer the widget-scoped entry (radio export / TU); fall back to
            # plain name for older callers that keyed label_data by name only.
            entry = label_data.get(wkey) or label_data.get(name) or {}
            lbl = (entry.get("label") or "").strip()
            if not lbl:
                lbl = tu_by_name.get(name, "")     # authoritative AcroForm tooltip
            if not lbl:
                # Radio option: the export value itself is a better label than
                # a cryptic shared field name ("Male" beats "undefined_3").
                if f.get("_radio_group") and f.get("export_value"):
                    lbl = str(f["export_value"]).strip()
            if not lbl:
                lbl = name
            parts = []
            sec = entry.get("section")
            if sec and sec.lower() not in lbl.lower():
                parts.append(sec)
            grp = entry.get("group")
            if grp and grp.lower() not in lbl.lower():
                parts.append(grp)
            parts.append(lbl)
            return " / ".join(parts)

        return {map_field_key(f): _ai_label(f) for f in fields_info if f.get("name")}

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
        _label_data = self._extract_labels_for_fields(fillable_pdf_path, fields_info)
        field_labels = self._flatten_field_labels(fields_info, _label_data)
        print("  🏷️  Field labels detected:")
        for f in fields_info:
            ftype = "cb" if "/Btn" in f["type"] else "tx"
            src = _label_data.get(f["name"], {}).get("source", "?")
            print(f"       [{ftype}] {f['name']:<25} → {field_labels.get(f['name'], '?')}  [{src}]")

        # Step 3a — canonical fork (canonical-first, PHI-free, critical deferral).
        # Fills every field it can place through the FIXED canonical schema; the
        # remaining fields fall through to the general Call-3 mapper below.
        canon_values: Dict[str, str] = {}
        canon_conf: Dict[str, float] = {}
        canon_report: dict = {"filled": 0, "deferred": [], "mapped_fields": 0}
        if settings.CANONICAL_FORK_ENABLED:
            try:
                canon_values, canon_conf, canon_report = self._canonical_fork(
                    fields_info, field_labels, user_data
                )
                if canon_values or canon_report.get("deferred"):
                    print(
                        f"  🧭  Canonical fork: filled {len(canon_values)}, "
                        f"deferred {len(canon_report.get('deferred', []))} critical "
                        f"(of {canon_report.get('mapped_fields', 0)} canonical fields)"
                    )
            except Exception as e:
                print(f"  ⚠️  canonical fork skipped: {e}")
                canon_values, canon_conf = {}, {}
                canon_report = {"filled": 0, "deferred": [], "mapped_fields": 0}

        # Fields the canonical fork already placed OR deliberately deferred must
        # be kept away from the general fork (never guess a deferred critical).
        deferred_names = {d["field"] for d in canon_report.get("deferred", [])}
        skip_names = set(canon_values.keys()) | deferred_names
        remaining_info = [f for f in fields_info if f["name"] not in skip_names]
        remaining_labels = {
            k: v for k, v in field_labels.items() if k not in skip_names
        }

        # Step 3b — general AI semantic matching (Call 3) for the remaining fields
        if remaining_info:
            gen_values, gen_conf, cache_hit = self._map_fields_with_ai(
                remaining_info, remaining_labels, user_data
            )
        else:
            gen_values, gen_conf, cache_hit = {}, {}, False

        # Merge — canonical fork wins (disjoint from the general fork by design)
        field_values = {**gen_values, **canon_values}
        confidence = {**gen_conf, **canon_conf}

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

        # Step 3c — suppress single-value broadcast across repeating table rows
        # (a flat, single-valued input must not paint the same value into every
        # row of a "Drug Name"/"Ingredient"/… column).
        field_values, confidence, row_dropped = self._suppress_row_broadcast(
            field_values, confidence, fields_info, field_labels
        )
        if row_dropped:
            print(f"  🧹  Suppressed {len(row_dropped)} duplicated table-row "
                  f"cell(s): {row_dropped}")

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
            "canonical_filled": len(canon_values),
            "canonical_deferred": canon_report.get("deferred", []),
            "canonical_mapped_fields": canon_report.get("mapped_fields", 0),
            "canonical_map_reviewed": (
                self._canonical_service.is_reviewed(fields_info)
                if settings.CANONICAL_FORK_ENABLED else False
            ),
            "row_broadcast_suppressed": row_dropped,
            "error": None if success else "Failed to write filled PDF",
        }
