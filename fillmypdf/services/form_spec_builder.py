"""
form_spec_builder.py
====================
Build a :class:`~fillmypdf.models.form_spec.FormSpec` from a blank form's
widgets plus the rich label record produced by label extraction.

The label record (``{widget_key: {label, section, subsection, group,
conditional, skip_logic, …}}``) already carries a form's whole question
structure — Gemini's full pass resolves ``group`` for the great majority of
checkbox widgets. Previously only the flat ``label`` string survived into the
canonical map and everything else was dropped at the boundary; this module is
what keeps the rest.

Grouping rules, in priority order:

1. Widgets sharing one AcroForm field with distinct exports are a PDF-enforced
   radio group — the field name is the authority, whatever the printed text says.
2. Otherwise checkboxes are grouped by their ``group`` question, scoped to
   section + subsection so a generic "Select one:" appearing twice on a form
   stays two questions.
3. A checkbox with no group question stands alone as a single yes/no box.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ..models.form_spec import (
    ExtraField,
    FormSpec,
    FormTable,
    LongTextField,
    QuestionGroup,
    QuestionOption,
    SignatureField,
    TableColumn,
    VisibilityRule,
)
from ..models.pa_canonical import BY_PATH, acro_field_name, map_field_key
from .field_classifier import (
    CHOICE,
    DATA,
    LONGTEXT,
    SIGNATURE,
    classify_fields,
    field_kind,
    is_section_title_field,
)

# Max horizontal gap (PDF points) between a checkbox and an inline blank that
# belongs to it — mirrors the inspect-row linkage used by extract.
_GAP_MAX = 260

# Printed instructions that mean single-select even when the PDF authored the
# boxes as independent fields (so nothing stops a user ticking several).
_SINGLE_SELECT_RE = re.compile(r"\b(select|choose|check|pick)\s+(only\s+)?one\b", re.I)
_SELECT_ALL_RE = re.compile(r"select\s+all\s+that\s+apply", re.I)
_SELECT_ONE_TITLE_RE = re.compile(r"^select\s+one\b", re.I)
_SECTION_PREFIX_RE = re.compile(r"^section\s+\d+", re.I)
_TRUNCATED_TOPIC_RE = re.compile(
    r"\b(is|the|a|an|of|for|to|and|or|with|by|this|patient)\s*$", re.I
)


# Positional ids the geometry labeler invents for boxes sharing a row
# ("group_3", "table_2"). They carry no meaning, so they must never surface as a
# question — a box with one of these is simply a box whose header wasn't found.
_SYNTHETIC_GROUP_RE = re.compile(r"^(group|table|section|col|row)[\s_]*\d+$", re.I)


def _norm_question(text: Optional[str]) -> str:
    """Collapse whitespace, drop a trailing colon, reject placeholder ids.

    Forms label the same question inconsistently ("Detailed Category" and
    "Detailed Category:" both appear on one BCBSTX form), which would otherwise
    split one question into two.
    """
    s = re.sub(r"\s+", " ", (text or "").strip())
    s = s.rstrip(":").strip()
    return "" if _SYNTHETIC_GROUP_RE.match(s) else s


def _slug(text: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:60] or "question"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}_{n}", n + 1
    taken.add(slug)
    return slug


def link_inline_blanks(fields_info: List[dict]) -> Dict[str, str]:
    """Map each inline text blank → the AcroForm name of its owning checkbox.

    A text field on the same printed row as a checkbox, sitting to its right
    (the date box in "[ ] Continuation of therapy (date initiated: ___)"), is
    that checkbox's conditional input. Returns ``{text_field_name: checkbox_name}``.
    """
    checkboxes = [f for f in fields_info if "/Btn" in str(f.get("type", ""))]
    out: Dict[str, str] = {}
    for tf in fields_info:
        if "/Tx" not in str(tf.get("type", "")):
            continue
        ty0, ty1 = tf["y"], tf.get("y_bottom", tf["y"])
        best_name, best_gap = None, float("inf")
        for c in checkboxes:
            if c["page"] != tf["page"]:
                continue
            cy0, cy1 = c["y"], c.get("y_bottom", c["y"])
            if cy1 < ty0 or cy0 > ty1:
                continue
            if c["x1"] > tf["x0"] + 4:
                continue
            gap = tf["x0"] - c["x1"]
            if 0 <= gap <= _GAP_MAX and gap < best_gap:
                best_gap, best_name = gap, c["name"]
        if best_name:
            out[tf["name"]] = best_name
    return out


def _parse_conditional(
    prose: Optional[str], option_index: Dict[str, str]
) -> Optional[VisibilityRule]:
    """Turn a prose hint ("Only if 'Other' is checked") into an executable rule.

    Deliberately conservative: a rule is emitted only when the referenced option
    text matches a real option on this form. Otherwise the prose is kept for
    display and a reviewer decides, which is safer than inventing a rule that
    silently hides a field.
    """
    if not prose:
        return None
    quoted = re.findall(r"['\"\u2018\u2019\u201c\u201d]([^'\"\u2018\u2019\u201c\u201d]{2,40})['\"\u2018\u2019\u201c\u201d]", prose)
    candidates = [q.strip() for q in quoted]
    m = re.search(r"\bif\s+(?:the\s+)?([A-Za-z][A-Za-z /-]{1,38})\b", prose, re.I)
    if m:
        candidates.append(m.group(1).strip())
    for cand in candidates:
        key = option_index.get(_norm_question(cand).lower())
        if key:
            return VisibilityRule(
                field=key, equals=None, source="conditional_text", raw=prose
            )
    return None


_SEX_OPTS = {"male", "female", "other", "unknown", "m", "f", "non-binary", "nonbinary"}
# Max gap (in PDF reading-order index) between two solo boxes still considered
# the same printed question. Larger gaps usually mean a different block.
_SOLO_ORDER_GAP = 8


def is_solo_question(q: QuestionGroup) -> bool:
    """True when question text is just a copy of its only option (no real header)."""
    if len(q.options) != 1:
        return False
    return _norm_question(q.options[0].label).lower() == _norm_question(q.question).lower()


def infer_cluster_title(
    option_labels: List[str],
    *,
    section: Optional[str] = None,
    subsection: Optional[str] = None,
) -> str:
    """Pick a human question title for a run of header-less options."""
    labs = [_norm_question(x).lower() for x in option_labels if x]
    labset = set(labs)
    if labset & {"male", "female", "m", "f"} and labset <= _SEX_OPTS:
        return "Sex"
    if any("new therapy" in x or x == "new" for x in labs) and any(
        "continuation" in x for x in labs
    ):
        return "Type of therapy"
    if labset == {"yes", "no"}:
        return _norm_question(subsection) or "Yes / No"
    if subsection and 1 <= len(subsection.split()) <= 10:
        return _norm_question(subsection)
    if section:
        leaf = re.split(r"[—–|-]", section)[-1].strip()
        leaf = _norm_question(leaf)
        if leaf and len(leaf) < 60:
            return f"Select one — {leaf}"
    return "Select one"


def merge_question_groups(
    groups: List[QuestionGroup],
    *,
    question: Optional[str] = None,
    input_type: Optional[str] = None,
    taken: Optional[set] = None,
) -> QuestionGroup:
    """Combine several question cards into one (options concatenated, PDF order)."""
    if not groups:
        raise ValueError("nothing to merge")
    taken = taken if taken is not None else set()
    groups = sorted(groups, key=lambda q: q.order)
    opts: List[QuestionOption] = []
    for g in groups:
        for o in g.options:
            opts.append(
                QuestionOption(
                    field=o.field,
                    acro_field=o.acro_field,
                    export=o.export,
                    label=o.label,
                    order=len(opts),
                    skip_logic=getattr(o, "skip_logic", None),
                )
            )
    labels = [o.label for o in opts]
    title = _norm_question(question) or infer_cluster_title(
        labels, section=groups[0].section, subsection=groups[0].subsection
    )
    # Drop the old slugs so the new id can reuse a clean name.
    for g in groups:
        taken.discard(g.id)
    single = (
        input_type == "radio"
        or (input_type is None and (
            _SINGLE_SELECT_RE.search(title or "")
            or infer_cluster_title(labels) in ("Sex", "Type of therapy", "Yes / No")
            or title in ("Sex", "Type of therapy", "Yes / No")
        ))
    )
    return QuestionGroup(
        id=_slug(title, taken),
        question=title,
        input="radio" if single else (input_type or "checkbox"),
        options=opts,
        section=groups[0].section,
        subsection=groups[0].subsection,
        page=groups[0].page,
        order=groups[0].order,
        conditional=next((g.conditional for g in groups if g.conditional), None),
        skip_logic=next((g.skip_logic for g in groups if g.skip_logic), None),
        canonical_hint=next((g.canonical_hint for g in groups if g.canonical_hint), None),
    )


def cluster_solo_questions(
    questions: List[QuestionGroup],
    taken: Optional[set] = None,
) -> List[QuestionGroup]:
    """Merge adjacent same-section solo cards into multi-option questions."""
    taken = taken if taken is not None else {q.id for q in questions}
    out: List[QuestionGroup] = []
    i = 0
    n = len(questions)
    while i < n:
        q = questions[i]
        if not is_solo_question(q):
            out.append(q)
            i += 1
            continue
        run = [q]
        j = i + 1
        while j < n:
            nxt = questions[j]
            if not is_solo_question(nxt):
                break
            if nxt.page != q.page or (nxt.section or "") != (q.section or ""):
                break
            if nxt.order - run[-1].order > _SOLO_ORDER_GAP:
                break
            run.append(nxt)
            j += 1
        if len(run) == 1:
            out.append(q)
        else:
            out.append(merge_question_groups(run, taken=taken))
        i = j
    return out


def _is_pdf_enforced_radio(q: QuestionGroup) -> bool:
    """True when options share one AcroForm name with distinct exports."""
    if len(q.options) < 2:
        return False
    acros = {o.acro_field for o in q.options}
    return len(acros) == 1


def _looks_like_new_refill(q: QuestionGroup) -> bool:
    blob = " ".join((o.label or "") for o in q.options).lower()
    return ("refill" in blob) and ("new" in blob or "prescription" in blob)


def _yn_option_pair(
    q: QuestionGroup,
) -> Optional[Tuple[QuestionOption, QuestionOption]]:
    """Return (yes, no) options when this is a two-way Yes/No question."""
    if len(q.options) != 2:
        return None
    a, b = q.options[0], q.options[1]

    def _side(o: QuestionOption) -> Optional[str]:
        ex = (o.export or "").strip().lower()
        if ex in ("y", "yes", "true", "1"):
            return "yes"
        if ex in ("n", "no", "false", "0"):
            return "no"
        lab = _norm_question(o.label).lower()
        if lab in ("yes", "y") or lab.startswith("yes"):
            return "yes"
        if lab in ("no", "n") or lab.startswith("no"):
            return "no"
        return None

    sa, sb = _side(a), _side(b)
    if {sa, sb} == {"yes", "no"}:
        return (a, b) if sa == "yes" else (b, a)
    return None


def _strip_section_prefix(text: Optional[str], section: Optional[str]) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    if not s:
        return s
    if section:
        sec = section.strip()
        if sec and s.lower().startswith(sec.lower()):
            rest = s[len(sec) :].lstrip(" -–—:")
            if rest:
                return rest
    m = re.match(
        r"^section\s+\d+[^:]*:?\s*[-–—]\s*(.+)$",
        s,
        re.I,
    )
    if m:
        return m.group(1).strip()
    return s


def _title_from_acro(name: Optional[str]) -> str:
    s = re.sub(r"\s+", " ", (name or "").strip())
    if not s:
        return "Yes / No"
    return s[:1].upper() + s[1:]


def merge_select_all_orphans(
    questions: List[QuestionGroup],
    taken: Optional[set] = None,
) -> List[QuestionGroup]:
    """Fold stray multi-criteria boxes into a nearby Select All That Apply.

    Extract sometimes labels later IR/ER/fentanyl siblings as ``Select one``
    (CareFirst methadone / MME / qty-limit), which becomes a false radio group.
    Those independent checkboxes belong with the printed multi-select.
    """
    taken = taken if taken is not None else {q.id for q in questions}
    anchors = [q for q in questions if _SELECT_ALL_RE.search(q.question or "")]
    if not anchors:
        return questions

    absorb: set = set()
    for anchor in anchors:
        sec = anchor.section or ""
        for q in questions:
            if q is anchor or q.id in absorb:
                continue
            if (q.section or "") != sec:
                continue
            if _SELECT_ALL_RE.search(q.question or ""):
                continue
            if _is_pdf_enforced_radio(q) or _looks_like_new_refill(q):
                continue
            if _yn_option_pair(q):
                continue
            take = False
            if is_solo_question(q) and q.input == "checkbox":
                take = True
            elif _SELECT_ONE_TITLE_RE.search(q.question or "") and not _is_pdf_enforced_radio(q):
                # Mis-captioned independent checkboxes (export On / distinct /T).
                take = True
            if not take:
                continue
            for o in q.options:
                anchor.options.append(
                    QuestionOption(
                        field=o.field,
                        acro_field=o.acro_field,
                        export=o.export,
                        label=o.label,
                        order=len(anchor.options),
                        skip_logic=getattr(o, "skip_logic", None),
                    )
                )
            taken.discard(q.id)
            absorb.add(q.id)
        anchor.input = "checkbox"

    if not absorb:
        return questions
    return [q for q in questions if q.id not in absorb]


def polish_yes_no_questions(
    questions: List[QuestionGroup],
    taken: Optional[set] = None,
) -> List[QuestionGroup]:
    """Fix Yes/No cards whose labels were polluted with section headers.

    CareFirst extract often yields ``SECTION 4: … - Yes`` / ``No Active Cancer…``
    with the section string reused as the question title. Skips sex / review
    radios that only happen to use Y/N export values.
    """
    taken = taken if taken is not None else {q.id for q in questions}
    for q in questions:
        pair = _yn_option_pair(q)
        if not pair:
            continue
        yes_o, no_o = pair
        raw_labs = {_norm_question(o.label).lower() for o in q.options}
        if raw_labs & _SEX_OPTS:
            continue
        # Require at least one label that looks like Yes/No prose (not just
        # a Y/N export on Male/Female or Non-Urgent/Urgent).
        yes_raw = _norm_question(yes_o.label).lower()
        no_raw = _norm_question(no_o.label).lower()
        label_yn = (
            yes_raw in ("yes", "y")
            or yes_raw.startswith("yes")
            or _SECTION_PREFIX_RE.match(yes_raw or "")
            or no_raw in ("no", "n")
            or no_raw.startswith("no ")
        )
        if not label_yn:
            continue

        yes_lab = _strip_section_prefix(yes_o.label, q.section)
        no_lab = _strip_section_prefix(no_o.label, q.section)
        topic = None
        m = re.match(r"^no\s+(.+)$", no_lab, re.I)
        if m:
            topic = m.group(1).strip()

        q_text = _norm_question(q.question)
        polluted = bool(
            _SECTION_PREFIX_RE.match(q_text)
            or (q.section and q_text.lower().startswith((q.section or "").lower()[:24]))
            or " — " in (q.question or "")
        )
        # Always tidy obvious section-prefixed option labels.
        if re.match(r"^yes\b", yes_lab, re.I) or yes_lab.lower() in ("y",):
            yes_o.label = "Yes"
        if m or no_lab.lower() in ("no", "n"):
            no_o.label = "No"

        if not polluted:
            continue
        topic_ok = bool(
            topic
            and len(topic) > 3
            and not _TRUNCATED_TOPIC_RE.search(topic)
            and not topic.rstrip().endswith(("/", "[", "(", "-", "–", "—"))
            and not ("[" in topic and "]" not in topic)
        )
        if topic_ok:
            new_title = topic
        else:
            new_title = _title_from_acro(yes_o.acro_field)
        if new_title and new_title != q.question:
            taken.discard(q.id)
            q.question = new_title
            q.id = _slug(new_title, taken)
    return questions


# TDI / Texas-style grids name cells ``… Row 1`` / ``…_Row_2`` instead of
# ``…_2``. Strip those so column families and headers stay clean.
_ROW_SUFFIX_RE = re.compile(
    r"(?:\s+row\s*\d+|[_-]row[_-]?\d+)\s*$",
    re.I,
)


def _field_base_name(name: str) -> str:
    """Strip trailing row suffixes for table column families.

    Handles ``Drug Name_2``, ``Drug Name-0``, and ``Planned Service Row 1`` /
    ``Start Date_Row_3``. Pure-numeric AcroForm names (``46``, ``51``) are
    returned unchanged — stripping digits alone used to collapse every cell
    to ``""`` and broke sibling merge on Aetna TX-style forms.
    """
    s = (name or "").strip()
    if not s:
        return ""
    spaced = _ROW_SUFFIX_RE.sub("", s).rstrip("_- ").rstrip()
    if spaced and spaced != s:
        return spaced
    stripped = re.sub(r"([_-]\d+|\d+)$", "", s).rstrip("_-").rstrip()
    return stripped if stripped else s


def _norm_col_header(text: Optional[str]) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip().lower())
    s = _ROW_SUFFIX_RE.sub("", s).rstrip("_- ").rstrip()
    return s.rstrip(":").strip()


def _pretty_col_header(text: Optional[str]) -> str:
    """Humanize a jammed AcroForm / label header for table display."""
    s = re.sub(r"\s+", " ", (text or "").strip())
    # Drop ``Row 1`` / ``_Row_2`` so Guided Fill shows column titles, not cell names.
    s = _ROW_SUFFIX_RE.sub("", s).rstrip("_- ").rstrip()
    # Drug NameStrengthDose → Drug Name/Strength/Dose
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", "/", s)
    return s


def _shorten_shared_column_headers(headers: List[str]) -> Dict[str, str]:
    """Strip a shared leading phrase across columns (TDI-style long cell names).

    ``Planned Service or Procedure`` / ``… Code`` / ``… Start Date`` →
    ``Procedure`` / ``Code`` / ``Start Date``.
    """
    cleaned = [re.sub(r"\s+", " ", (h or "").strip()) for h in headers]
    if len(cleaned) < 2:
        return {h: h for h in headers}
    tokenized = [h.split() for h in cleaned]
    if not all(tokenized):
        return {h: h for h in headers}
    prefix_len = 0
    for toks in zip(*tokenized):
        if len({t.lower() for t in toks}) == 1:
            prefix_len += 1
        else:
            break
    if prefix_len < 2:
        return {h: h for h in headers}
    out: Dict[str, str] = {}
    for orig, toks in zip(headers, tokenized):
        if len(toks) > prefix_len:
            short = " ".join(toks[prefix_len:])
        else:
            short = toks[-1]
        if len(short) < 2:
            return {h: h for h in headers}
        out[orig] = short
    return out


def _field_x_center(f: dict) -> float:
    x0 = float(f.get("x0") or f.get("x") or 0)
    x1 = float(f.get("x1") or x0)
    return (x0 + x1) / 2.0


def _synthetic_column_seeds(
    data_fields: List[dict],
    rec,
) -> List[tuple]:
    """Invent column seeds when extract set ``table`` but left ``column`` null.

    CareFirst-style history grids name cells ``Drug NameStrengthDose`` /
    ``Drug NameStrengthDose-0`` with a shared ``table`` and no column caption.
    Cluster by basename family; require ≥2 columns with ≥2 rows each.
    """
    by_table: "OrderedDict[str, List[dict]]" = OrderedDict()
    for f in data_fields:
        e = rec(f)
        tbl = (e.get("table") or "").strip()
        if not tbl or (e.get("column") or "").strip():
            continue
        by_table.setdefault(tbl, []).append(f)

    seeds: List[tuple] = []
    for _tbl, members in by_table.items():
        by_base: "OrderedDict[str, List[dict]]" = OrderedDict()
        for f in members:
            base = _field_base_name(f["name"])
            if not base or base.isdigit():
                continue
            by_base.setdefault(base, []).append(f)
        families = [(b, fs) for b, fs in by_base.items() if len(fs) >= 2]
        if len(families) < 2:
            continue
        for base, fs in families:
            # Prefer cleaned basename so headers are ``Procedure Code``, not
            # ``… Code Row 1`` (TDI / Texas NOFR grids).
            header = _pretty_col_header(base) or _pretty_col_header(
                (rec(fs[0]).get("label") or "")
            )
            for f in fs:
                seeds.append((f, rec(f), header))
    return seeds


def build_form_tables(
    fields_info: List[dict],
    label_data: Dict[str, dict],
    *,
    widget_key=None,
    taken: Optional[set] = None,
) -> List[FormTable]:
    """Detect repeating grids from extract ``table`` / ``column`` metadata.

    Headers prefer the printed ``column`` caption. Sibling row cells that only
    have a tooltip (``acroform-tu``) are pulled in by:

    1. basename family (``Drug Name_2`` → ``Drug Name``), or
    2. same extract ``table`` name + label matching a seeded column header, or
    3. same ``table`` + nearest seeded column by x-position.

    When extract marks ``table`` but never sets ``column``, column headers are
    inferred from repeating basename families (CareFirst opioid history grid).
    """
    if widget_key is None:
        def widget_key(f: dict) -> str:
            return f["name"]

    if taken is None:
        taken = set()

    def rec(f: dict) -> dict:
        return label_data.get(widget_key(f)) or label_data.get(f["name"]) or {}

    from .field_classifier import field_kind as _field_kind

    data_fields = [
        f for f in fields_info
        if f.get("name") and _field_kind(f) == DATA
    ]

    # Seeds: widgets that extract marked with a column header.
    seeds: List[tuple] = []
    for f in data_fields:
        e = rec(f)
        col = (e.get("column") or "").strip()
        if col:
            seeds.append((f, e, col))

    if not seeds:
        seeds = _synthetic_column_seeds(data_fields, rec)

    if not seeds:
        return []

    # Family bases that belong to a seeded column (Drug Name_2 → Drug Name).
    # Skip pure-numeric bases — they are not reusable families.
    base_header: Dict[str, str] = {}
    for f, e, col in seeds:
        base = _field_base_name(f["name"])
        if base and not base.isdigit():
            base_header[base] = col

    def _grid_key(e: dict) -> str:
        """One printed grid — table name, split by subsection when present.

        Aetna Section 8 puts both the prior-therapy grid and the lab-values
        grid under the same ``table`` string; subsection keeps them apart.
        """
        tbl = (e.get("table") or "").strip()
        sub = (e.get("subsection") or "").strip()
        if tbl and sub:
            return f"{tbl}||{sub}"
        return tbl or sub or (e.get("section") or "").strip() or "table"

    # Per-grid column vocabulary + x-centers from seeds (for orphan cells).
    grid_headers: Dict[str, "OrderedDict[str, str]"] = {}  # gkey → norm→header
    grid_col_xs: Dict[str, List[Tuple[float, str]]] = {}  # gkey → [(x, header)]
    header_to_grids: Dict[str, List[str]] = {}  # display header → [gkey, ...]
    for f, e, col in seeds:
        gkey = _grid_key(e)
        if gkey == "table" and not (e.get("table") or e.get("subsection")):
            continue
        headers = grid_headers.setdefault(gkey, OrderedDict())
        headers[_norm_col_header(col)] = col
        grid_col_xs.setdefault(gkey, []).append((_field_x_center(f), col))
        header_to_grids.setdefault(col, []).append(gkey)

    # Table-name → grids (for orphans that only have ``table``, no subsection).
    table_to_grids: Dict[str, List[str]] = {}
    for gkey in grid_headers:
        tbl = gkey.split("||", 1)[0]
        table_to_grids.setdefault(tbl, []).append(gkey)

    def _resolve_grid(e: dict, header: str) -> Optional[str]:
        gkey = _grid_key(e)
        if gkey in grid_headers and len(grid_headers[gkey]) >= 2:
            return gkey
        tbl = (e.get("table") or "").strip()
        candidates = table_to_grids.get(tbl) or header_to_grids.get(header) or []
        # Prefer the grid that already owns this column header.
        owning = [
            g for g in candidates
            if header in grid_headers.get(g, {}).values()
            or _norm_col_header(header) in grid_headers.get(g, {})
        ]
        pool = owning or [g for g in candidates if len(grid_headers.get(g, {})) >= 2]
        if len(pool) == 1:
            return pool[0]
        if len(pool) > 1:
            # Prefer grid whose subsection matches when extract has one.
            sub = (e.get("subsection") or "").strip()
            if sub:
                for g in pool:
                    if g.endswith(f"||{sub}") or g == sub:
                        return g
            return pool[0]
        return gkey if gkey in grid_headers else None

    def _infer_header(f: dict, e: dict) -> Optional[str]:
        """Column header for a cell that extract left without ``column``."""
        name = f["name"]
        base = _field_base_name(name)
        if base in base_header:
            return base_header[base]

        tbl = (e.get("table") or "").strip()
        gkeys = table_to_grids.get(tbl) or []
        if not gkeys:
            return None

        label_n = _norm_col_header(e.get("label") or e.get("column") or "")
        if label_n:
            for gkey in gkeys:
                headers = grid_headers.get(gkey) or {}
                if label_n in headers:
                    return headers[label_n]
                for nh, header in headers.items():
                    if nh and (nh in label_n or label_n in nh):
                        return header

        # Named cells that are not part of any seeded basename family must not
        # be snapped by x alone — that pulled TDI's ICD Version Number into the
        # Diagnosis Code column. Pure-numeric widgets still use x (Aetna TX).
        if base and not base.isdigit() and base not in base_header:
            base_n = _norm_col_header(base)
            related = False
            for gkey in gkeys:
                for nh in (grid_headers.get(gkey) or {}):
                    if nh and (nh in base_n or base_n in nh):
                        related = True
                        break
                if related:
                    break
            if not related:
                return None

        # Nearest seeded column by horizontal position across grids in table.
        xs: List[Tuple[float, str]] = []
        for gkey in gkeys:
            xs.extend(grid_col_xs.get(gkey) or [])
        if not xs:
            return None
        cx = _field_x_center(f)
        by_h: Dict[str, List[float]] = {}
        for x, h in xs:
            by_h.setdefault(h, []).append(x)
        best_h, best_d = None, float("inf")
        for h, vals in by_h.items():
            mx = sum(vals) / len(vals)
            d = abs(cx - mx)
            if d < best_d:
                best_d, best_h = d, h
        return best_h

    # Group by grid key so history rows stay together, without merging two
    # different grids that share one section-level ``table`` name.
    groups: "OrderedDict[str, List[tuple]]" = OrderedDict()
    for f in data_fields:
        e = rec(f)
        header = (e.get("column") or "").strip() or _infer_header(f, e)
        if not header:
            continue
        gkey = _resolve_grid(e, header)
        if not gkey or gkey not in grid_headers:
            continue
        if len(grid_headers[gkey]) < 2:
            continue
        groups.setdefault(gkey, []).append((f, e, header))

    tables: List[FormTable] = []
    for gkey, members in groups.items():
        # column header → [(y, name), ...]
        cols: "OrderedDict[str, List[Tuple[float, str]]]" = OrderedDict()
        for f, e, header in members:
            y = float(f.get("y0") or f.get("y") or 0)
            cols.setdefault(header, []).append((y, f["name"]))

        if len(cols) < 2:
            continue  # need a real grid, not a single repeating column alone

        short_map = _shorten_shared_column_headers(list(cols.keys()))
        columns: List[TableColumn] = []
        row_count = 0
        for header, cells in cols.items():
            cells.sort(key=lambda t: t[0])
            names: List[str] = []
            seen = set()
            for _, n in cells:
                if n not in seen:
                    seen.add(n)
                    names.append(n)
            if len(names) < 2:
                continue
            display = short_map.get(header) or header
            columns.append(
                TableColumn(
                    id=_slug(display, taken),
                    header=display,
                    fields=names,
                )
            )
            row_count = max(row_count, len(names))

        if len(columns) < 2 or row_count < 2:
            continue

        # Prefer a lettered subsection (D. …) as the human title.
        title = None
        subsection = None
        section = None
        for _, e, _ in members:
            sub = e.get("subsection")
            if sub and re.match(r"^\s*[A-Z]\.\s+", str(sub)):
                title = sub
                subsection = sub
                section = e.get("section")
                break
        if not title:
            first_e = members[0][1]
            title = first_e.get("subsection") or first_e.get("table") or gkey
            subsection = first_e.get("subsection")
            section = first_e.get("section")

        cell_names = {n for c in columns for n in c.fields}
        order = next(
            (i for i, f in enumerate(fields_info) if f.get("name") in cell_names),
            0,
        )
        first_f = next(f for f, _, _ in members if f["name"] in cell_names)
        tables.append(
            FormTable(
                id=_slug(title or "table", taken),
                title=title,
                section=section,
                subsection=subsection,
                page=(first_f.get("page") or 0) + 1,
                order=order,
                columns=columns,
                row_count=row_count,
            )
        )

    tables.sort(key=lambda t: t.order)
    return tables


_SIGNER_ROLE_RE = re.compile(
    r"\b(prescriber|physician|provider|doctor|patient|member|applicant|"
    r"representative|pharmacist|witness|subscriber)\b",
    re.I,
)
_SIG_DATE_NAME_RE = re.compile(
    r"\bdate\b|mm\s*/?\s*dd|signed",
    re.I,
)
_NOT_SIG_DATE_RE = re.compile(
    r"\b(dob|birth|date\s+of\s+birth|onset|start\s+date|end\s+date|"
    r"effective|expiration|fill\s+date|service\s+date|duration)\b",
    re.I,
)
# Same-page date blank within this many PDF points of a signature line.
_SIG_DATE_Y_GAP = 36.0
_SIG_DATE_X_GAP = 320.0


def _infer_signer_role(label: str) -> Optional[str]:
    m = _SIGNER_ROLE_RE.search(label or "")
    if not m:
        return None
    role = m.group(1).lower()
    if role in ("physician", "provider", "doctor"):
        return "prescriber"
    if role == "member":
        return "patient"
    if role == "subscriber":
        return "patient"
    return role


def _looks_like_signature_date(name: str, label: str = "") -> bool:
    blob = f"{name or ''} {label or ''}".strip()
    if not blob or _NOT_SIG_DATE_RE.search(blob):
        return False
    return bool(_SIG_DATE_NAME_RE.search(blob))


def _build_signature_fields(
    sig_widgets: List[dict],
    fields_info: List[dict],
    label_data: Dict[str, dict],
    *,
    widget_key,
    order_of: Dict[int, int],
) -> List[SignatureField]:
    """Assemble signature widgets and their adjacent date blanks."""

    def rec(f: dict) -> dict:
        return label_data.get(widget_key(f)) or label_data.get(f["name"]) or {}

    by_name = {f["name"]: f for f in fields_info if f.get("name")}
    signatures: List[SignatureField] = []
    taken_names: set = set()

    for f in sig_widgets:
        e = rec(f)
        label = e.get("label") or f["name"]
        signatures.append(
            SignatureField(
                field=map_field_key(f),
                acro_field=f["name"],
                label=label,
                section=e.get("section"),
                page=(f.get("page") or 0) + 1,
                order=order_of.get(id(f), 0),
                kind="signature",
                role=_infer_signer_role(label) or _infer_signer_role(f["name"]),
            )
        )
        taken_names.add(f["name"])

    # Companion date blanks: same page, near a signature line (CareFirst
    # ``Date inmddnryr`` next to ``Prescriber Signature``).
    for f in fields_info:
        name = f.get("name")
        if not name or name in taken_names:
            continue
        if field_kind(f) != DATA:
            continue
        e = rec(f)
        if not _looks_like_signature_date(name, e.get("label") or ""):
            continue
        fy = float(f.get("y0") or f.get("y") or 0)
        fx = float(f.get("x0") or f.get("x") or 0)
        fp = f.get("page")
        near = False
        for s in signatures:
            if s.kind != "signature":
                continue
            sf = by_name.get(s.acro_field)
            if not sf or sf.get("page") != fp:
                continue
            sy = float(sf.get("y0") or sf.get("y") or 0)
            sx = float(sf.get("x0") or sf.get("x") or 0)
            if abs(fy - sy) <= _SIG_DATE_Y_GAP and abs(fx - sx) <= _SIG_DATE_X_GAP:
                near = True
                break
        if not near:
            continue
        label = e.get("label") or name
        # Prefer a readable caption when extract left OCR garbage.
        if _looks_like_signature_date(name, "") and (
            not e.get("label") or e.get("label") == name
        ):
            label = "Date (mm/dd/yyyy)"
        signatures.append(
            SignatureField(
                field=map_field_key(f),
                acro_field=name,
                label=label,
                section=e.get("section"),
                page=(fp or 0) + 1,
                order=order_of.get(id(f), 0),
                kind="date",
                role=None,
            )
        )
        taken_names.add(name)

    signatures.sort(key=lambda s: s.order)
    return signatures


def signature_field_keys(spec: FormSpec) -> set:
    """Map keys / AcroForm names owned by FormSpec.signatures (incl. dates)."""
    keys: set = set()
    for s in spec.signatures or []:
        if s.field:
            keys.add(s.field)
        if s.acro_field:
            keys.add(s.acro_field)
    return keys


def _owned_form_spec_keys(spec: FormSpec) -> set:
    """AcroForm / map keys already claimed by questions, tables, narratives, sigs."""
    owned: set = set()
    for q in spec.questions or []:
        for o in q.options:
            if o.field:
                owned.add(o.field)
                owned.add(acro_field_name(o.field))
            if o.acro_field:
                owned.add(o.acro_field)
    owned |= set(spec.table_field_keys or ())
    for lt in spec.long_text or []:
        if lt.field:
            owned.add(lt.field)
        if lt.acro_field:
            owned.add(lt.acro_field)
    for s in spec.signatures or []:
        if s.field:
            owned.add(s.field)
        if s.acro_field:
            owned.add(s.acro_field)
    return owned


def _mapping_has_catalog_path(mappings: Dict[str, dict], *keys: str) -> bool:
    for k in keys:
        if not k:
            continue
        m = mappings.get(k)
        if not isinstance(m, dict):
            continue
        path = m.get("canonical")
        if path and path != "other" and path in BY_PATH:
            return True
    return False


def build_form_extras(
    fields_info: List[dict],
    label_data: Dict[str, dict],
    mappings: Dict[str, dict],
    spec: FormSpec,
    *,
    widget_key=None,
) -> List[ExtraField]:
    """Leftover widgets: unmapped / ``other`` / non-catalog, not FormSpec-owned.

    These become Guided Fill "Additional fields" so they can still be filled
    without forcing a shared catalog path.
    """
    if widget_key is None:
        def widget_key(f: dict) -> str:
            return f["name"]

    def rec(f: dict) -> dict:
        return label_data.get(widget_key(f)) or label_data.get(f["name"]) or {}

    owned = _owned_form_spec_keys(spec)
    extras: List[ExtraField] = []
    seen: set = set()

    for order, f in enumerate(fields_info):
        name = f.get("name")
        if not name:
            continue
        kind = field_kind(f)
        if kind == SIGNATURE:
            continue
        key = map_field_key(f)
        acro = acro_field_name(key) or str(name)
        if key in owned or acro in owned or str(name) in owned:
            continue
        if is_section_title_field(str(name), "") or is_section_title_field(acro, ""):
            continue
        if _mapping_has_catalog_path(mappings, key, str(name), acro):
            continue
        if key in seen or acro in seen:
            continue
        seen.add(key)
        seen.add(acro)

        e = rec(f)
        label = (
            (e.get("label") or "").strip()
            or str(name)
        )
        if kind == CHOICE:
            extra_kind = "checkbox"
        elif kind == LONGTEXT:
            extra_kind = "longtext"
        else:
            extra_kind = "text"
        extras.append(
            ExtraField(
                field=key,
                acro_field=acro,
                label=label,
                kind=extra_kind,
                section=e.get("section"),
                subsection=e.get("subsection"),
                page=(f.get("page") or 0) + 1 if f.get("page") is not None else None,
                order=order,
                export=f.get("export_value") if extra_kind == "checkbox" else None,
            )
        )
    return extras


def build_form_spec(
    fields_info: List[dict],
    label_data: Dict[str, dict],
    *,
    signature: str,
    form_label: Optional[str] = None,
    widget_key=None,
) -> FormSpec:
    """Assemble the per-form question/narrative/signature spec.

    ``label_data`` is keyed by widget key (``VisionService._widget_key``); pass
    that function as ``widget_key`` so lookups match. Falls back to the plain
    field name when omitted.
    """
    if widget_key is None:
        def widget_key(f: dict) -> str:  # pragma: no cover - trivial fallback
            return f["name"]

    def rec(f: dict) -> dict:
        return label_data.get(widget_key(f)) or label_data.get(f["name"]) or {}

    buckets = classify_fields(fields_info)
    order_of = {id(f): i for i, f in enumerate(fields_info)}
    linked = link_inline_blanks(fields_info)

    # ── Checkbox / radio → question groups ───────────────────────────────
    grouped: "OrderedDict[Tuple, List[dict]]" = OrderedDict()
    for f in buckets[CHOICE]:
        e = rec(f)
        if f.get("_radio_group"):
            key = ("radio", f["name"])
        else:
            q = _norm_question(e.get("group"))
            # Header-less boxes stand alone, keyed by POSITION rather than field
            # name: forms routinely reuse one name ("undefined") across unrelated
            # widgets, and keying by name would merge them into one bogus question.
            key = (
                ("group", e.get("section") or "", e.get("subsection") or "", q.lower())
                if q
                else ("solo", order_of[id(f)])
            )
        grouped.setdefault(key, []).append(f)

    taken: set = set()
    questions: List[QuestionGroup] = []
    for key, members in grouped.items():
        first = rec(members[0])
        if key[0] == "solo":
            question = first.get("label") or members[0]["name"]
        else:
            question = _norm_question(first.get("group")) or (
                first.get("label") or members[0]["name"]
            )

        options: List[QuestionOption] = []
        for i, f in enumerate(members):
            e = rec(f)
            options.append(
                QuestionOption(
                    field=map_field_key(f),
                    acro_field=f["name"],
                    export=f.get("export_value"),
                    label=(e.get("label") or f.get("export_value") or f["name"]),
                    order=i,
                    skip_logic=e.get("skip_logic"),
                )
            )

        pdf_enforced = key[0] == "radio"
        questions.append(
            QuestionGroup(
                id=_slug(question, taken),
                question=question,
                input="radio" if (pdf_enforced or _SINGLE_SELECT_RE.search(question or "")) else "checkbox",
                options=options,
                section=first.get("section"),
                subsection=first.get("subsection"),
                page=(members[0].get("page") or 0) + 1,
                order=min(order_of[id(f)] for f in members),
                conditional=next((rec(f).get("conditional") for f in members if rec(f).get("conditional")), None),
                skip_logic=next((rec(f).get("skip_logic") for f in members if rec(f).get("skip_logic")), None),
            )
        )
    questions.sort(key=lambda q: q.order)

    # Header-less boxes that sit next to each other in the same section are
    # almost always ONE printed question (Sex: Male/Female, Type: New/Continuation).
    # Without a group caption they were stored as duplicate "solo" cards where
    # Question === Option. Cluster those runs so the webform gets a real header.
    questions = cluster_solo_questions(questions, taken)
    # Multi-criteria orphans mislabeled "Select one" → Select All That Apply.
    questions = merge_select_all_orphans(questions, taken)
    # Yes/No cards whose labels absorbed the section header.
    questions = polish_yes_no_questions(questions, taken)

    # One printed question can span several sub-columns ("Select Care Category"
    # over Physical Health / Behavioral Health / Pharmacy). Those stay separate
    # groups — that matches how the form is laid out — but identical wording
    # would be indistinguishable in a review list, so qualify each by section.
    counts: Dict[str, int] = {}
    for q in questions:
        counts[q.question] = counts.get(q.question, 0) + 1
    for q in questions:
        if counts.get(q.question, 0) > 1 and (q.section or q.subsection):
            q.question = f"{q.question} — {q.section or q.subsection}"

    # Option lookup for resolving prose conditions onto a concrete widget.
    option_index: Dict[str, str] = {}
    for q in questions:
        for o in q.options:
            option_index.setdefault(_norm_question(o.label).lower(), o.field)

    for q in questions:
        q.rule = _parse_conditional(q.conditional, option_index)

    # ── Multiline narratives ─────────────────────────────────────────────
    key_by_acro = {
        o.acro_field: o.field for q in questions for o in q.options
    }
    long_text: List[LongTextField] = []
    for f in buckets[LONGTEXT]:
        e = rec(f)
        owner = linked.get(f["name"])
        rule = None
        if owner and owner in key_by_acro:
            rule = VisibilityRule(
                field=key_by_acro[owner], equals=None, source="linked_field"
            )
        long_text.append(
            LongTextField(
                field=map_field_key(f),
                acro_field=f["name"],
                label=e.get("label") or f["name"],
                section=e.get("section"),
                subsection=e.get("subsection"),
                page=(f.get("page") or 0) + 1,
                order=order_of[id(f)],
                conditional=e.get("conditional"),
                skip_logic=e.get("skip_logic"),
                rule=rule or _parse_conditional(e.get("conditional"), option_index),
            )
        )

    # ── Signatures (/Sig + signature-line /Tx) + nearby date blanks ──────
    signatures = _build_signature_fields(
        buckets[SIGNATURE],
        fields_info,
        label_data,
        widget_key=widget_key,
        order_of=order_of,
    )

    # ── Repeating tables (history grids, etc.) — form-specific ───────────
    tables = build_form_tables(
        fields_info, label_data, widget_key=widget_key, taken=taken
    )

    return FormSpec(
        signature=signature,
        form_label=form_label,
        built_at=datetime.now().isoformat(),
        questions=questions,
        tables=tables,
        long_text=long_text,
        signatures=signatures,
        signatures_version=1,
    )


def promote_unresolved_long_text(
    spec: FormSpec,
    fields_info: List[dict],
    label_data: Dict[str, dict],
    unresolved_keys: set,
    *,
    widget_key=None,
) -> FormSpec:
    """Move wordy single-line fields the catalog couldn't place into the spec.

    Length alone never routes a field out of the canonical bucket — a verbose
    caption often still describes an ordinary catalog field. Only a field that
    is *both* long and unresolved becomes a form-specific narrative.
    """
    from .field_classifier import DATA, field_kind, is_long_question

    if widget_key is None:
        def widget_key(f: dict) -> str:  # pragma: no cover - trivial fallback
            return f["name"]

    have = {lt.field for lt in spec.long_text}
    order_of = {id(f): i for i, f in enumerate(fields_info)}
    for f in fields_info:
        key = map_field_key(f)
        if key in have or key not in unresolved_keys or field_kind(f) != DATA:
            continue
        e = label_data.get(widget_key(f)) or label_data.get(f["name"]) or {}
        label = e.get("label") or ""
        if not is_long_question(label):
            continue
        spec.long_text.append(
            LongTextField(
                field=key,
                acro_field=acro_field_name(key),
                label=label,
                section=e.get("section"),
                subsection=e.get("subsection"),
                page=(f.get("page") or 0) + 1,
                order=order_of[id(f)],
                conditional=e.get("conditional"),
                skip_logic=e.get("skip_logic"),
            )
        )
    spec.long_text.sort(key=lambda lt: lt.order)
    return spec
