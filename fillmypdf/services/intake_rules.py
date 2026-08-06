"""
intake_rules.py
===============
Turn extract-style ``linked_field`` / ``conditional`` / ``skip_logic`` into
executable show/enable rules for Guided Fill.

Extract already finds these (geometry link + Gemini prose). The canonical map
historically dropped them; this module is the shared place that:

1. Attaches presentation metadata onto canonical mapping entries.
2. Stores per-option ``skip_logic`` on form-spec question options.
3. Resolves a controlling AcroForm name / option label into a webform key
   (question id) so the UI can unlock dependent fields when Yes is selected —
   fields stay visible but locked until then.
4. Derives lettered skip cascades (A→B→C→D) so later questions and section-D
   tables unlock only on reachable paths.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from ..models.form_spec import FormSpec, RuleClause, VisibilityRule
from ..models.pa_canonical import (
    apply_label_role_to_path,
    apply_section_to_path,
    map_field_key,
)
from .field_classifier import (
    DATA,
    field_kind,
    field_kinds_for,
    is_section_title_field,
    prune_form_specific_mappings,
)
from .form_spec_builder import _norm_question, _parse_conditional, link_inline_blanks


def remap_mappings_by_section(
    mappings: Dict[str, dict],
    fields_info: List[dict],
    label_data: Dict[str, dict],
    *,
    widget_key=None,
) -> Tuple[Dict[str, dict], int]:
    """Flip patient/prescriber/pharmacy and provider *roles* via label + section.

    Fixes cached maps built before role-aware catalog matching. Returns
    ``(mappings, n_changed)``.
    """
    if widget_key is None:
        def widget_key(f: dict) -> str:
            return f["name"]

    changed = 0
    out: Dict[str, dict] = dict(mappings)
    for f in fields_info:
        name = f.get("name")
        if not name:
            continue
        key = map_field_key(f)
        entry = out.get(key) or out.get(name)
        if not isinstance(entry, dict):
            continue
        path = entry.get("canonical")
        if not path or path == "other":
            continue
        e = label_data.get(widget_key(f)) or label_data.get(name) or {}
        section = e.get("section")
        label = (e.get("label") or "").strip()
        new_path = apply_label_role_to_path(path, label, section)
        if not new_path or new_path == path:
            new_path = apply_section_to_path(path, section)
        if new_path and new_path != path:
            entry = dict(entry)
            entry["canonical"] = new_path
            out[key] = entry
            if name in out and name != key:
                out[name] = entry
            changed += 1
    return out, changed

# "B. Is this request…" / "item B" / "items B & C"
_LETTER_HEAD_RE = re.compile(r"^\s*([A-Z])\.\s+")
_GO_TO_RE = re.compile(r"go\s+to\s+item\s+([A-Z])\b", re.I)
_SKIP_ITEMS_RE = re.compile(
    r"skip\s+items?\s+([A-Z](?:\s*[,&]\s*[A-Z])*)",
    re.I,
)


def _yes_like(label: str) -> bool:
    low = _norm_question(label).lower()
    return low in {"yes", "y", "true", "on"} or low.startswith("yes")


def _no_like(label: str) -> bool:
    low = _norm_question(label).lower()
    return low in {"no", "n", "false", "off"} or low.startswith("no")


def _item_letter(*texts: Optional[str]) -> Optional[str]:
    for t in texts:
        if not t:
            continue
        m = _LETTER_HEAD_RE.match(str(t).strip())
        if m:
            return m.group(1).upper()
    return None


def _parse_skip_branch(text: Optional[str]) -> Tuple[Set[str], Optional[str]]:
    """Return (skipped_letters, go_to_letter) from printed skip_logic."""
    if not text:
        return set(), None
    raw = str(text)
    skipped: Set[str] = set()
    for m in _SKIP_ITEMS_RE.finditer(raw):
        for part in re.split(r"[,&]", m.group(1)):
            let = part.strip().upper()
            if len(let) == 1 and let.isalpha():
                skipped.add(let)
    go = None
    gm = _GO_TO_RE.search(raw)
    if gm:
        go = gm.group(1).upper()
    return skipped, go


def annotate_form_spec_options(
    spec: FormSpec,
    fields_info: List[dict],
    label_data: Dict[str, dict],
    *,
    widget_key=None,
) -> FormSpec:
    """Copy per-option skip_logic from the rich label record onto each option."""
    if widget_key is None:
        def widget_key(f: dict) -> str:
            return f["name"]

    by_key: Dict[str, dict] = {}
    for f in fields_info:
        if not f.get("name"):
            continue
        e = label_data.get(widget_key(f)) or label_data.get(f["name"]) or {}
        by_key[map_field_key(f)] = e
        by_key[f["name"]] = e

    for q in spec.questions:
        # Prefer the richest skip among options for the question-level badge,
        # but keep each option's own instruction (Yes vs No differ).
        skips = []
        for o in q.options:
            e = by_key.get(o.field) or by_key.get(o.acro_field) or {}
            skip = e.get("skip_logic")
            if skip:
                o.skip_logic = str(skip)
                skips.append(str(skip))
            cond = e.get("conditional")
            if cond and not q.conditional:
                q.conditional = str(cond)
        if skips and not q.skip_logic:
            # Show both branches when they differ (No's skip is the important one).
            uniq = list(dict.fromkeys(skips))
            q.skip_logic = " · ".join(uniq) if len(uniq) > 1 else uniq[0]
    return spec


def _controller_for_linked(
    linked_acro: str,
    spec: FormSpec,
) -> Optional[Tuple[str, Optional[str]]]:
    """Map an AcroForm checkbox name → (question_id, equals_label_or_None).

    ``equals`` is ``\"yes\"`` when the group has a Yes-like option (the common
    \"How Long? only if Yes\" pattern); otherwise None means \"any option checked\".
    """
    linked = (linked_acro or "").strip()
    if not linked:
        return None
    for q in spec.questions:
        for o in q.options:
            if o.acro_field == linked or o.field == linked or o.field.startswith(linked + "::"):
                equals = None
                for opt in q.options:
                    if _yes_like(opt.label) or (opt.export and _yes_like(opt.export)):
                        equals = opt.label  # keep printed casing for the dropdown
                        break
                return q.id, equals
    return None


def _yes_label(q) -> Optional[str]:
    for o in q.options:
        if _yes_like(o.label) or (o.export and _yes_like(o.export)):
            return o.label
    return None


def _no_label(q) -> Optional[str]:
    for o in q.options:
        if _no_like(o.label) or (o.export and _no_like(o.export)):
            return o.label
    return None


def _lettered_questions(spec: FormSpec) -> List[Tuple[str, object]]:
    """Questions participating in a lettered skip chain, in letter order.

    Prefer an explicit ``A.`` / ``B.`` prefix on the question or subsection.
    Otherwise infer from Yes-branch ``go to item X`` (that question is X-1).
    """
    out: List[Tuple[str, object]] = []
    for q in sorted(spec.questions, key=lambda qq: (qq.order, qq.id)):
        let = _item_letter(q.subsection, q.question)
        if not let:
            for o in q.options:
                if not (_yes_like(o.label) or (o.export and _yes_like(o.export))):
                    continue
                _, go = _parse_skip_branch(o.skip_logic)
                if go and len(go) == 1 and go.isalpha() and go != "A":
                    let = chr(ord(go) - 1)
                    break
        if not let:
            # Last question in a chain often only has go/skip to a section letter
            # (C → D). Infer from any option's go_to.
            for o in q.options:
                _, go = _parse_skip_branch(o.skip_logic)
                if go and len(go) == 1 and go.isalpha() and go != "A":
                    # Prefer Yes branch; fall back to first parseable.
                    if _yes_like(o.label) or not let:
                        let = chr(ord(go) - 1)
            if let and any(existing == let for existing, _ in out):
                let = None
        if let:
            out.append((let, q))
    out.sort(key=lambda t: t[0])
    # Dedup letters (keep earliest question).
    seen: Set[str] = set()
    uniq: List[Tuple[str, object]] = []
    for let, q in out:
        if let in seen:
            continue
        seen.add(let)
        uniq.append((let, q))
    return uniq


def _paths_reaching(
    lettered: List[Tuple[str, object]],
    target: str,
) -> List[List[Tuple[str, str]]]:
    """DFS answer paths (question_id, option_label) that visit ``target`` letter."""
    if not lettered:
        return []

    by_letter = {let: q for let, q in lettered}
    letters = [let for let, _ in lettered]
    paths: List[List[Tuple[str, str]]] = []

    # Target is the first question → always unlocked.
    if target == letters[0]:
        return [[]]

    def dfs(idx: int, path: List[Tuple[str, str]]) -> None:
        if idx >= len(letters):
            return
        let, q = lettered[idx]
        if let == target:
            paths.append(list(path))
            return
        for o in q.options:
            skipped, go = _parse_skip_branch(o.skip_logic)
            if not o.skip_logic:
                if _yes_like(o.label) and idx + 1 < len(letters):
                    go = letters[idx + 1]
                    skipped = set()
                else:
                    continue
            if target in skipped:
                continue
            if go == target:
                paths.append(path + [(q.id, o.label)])
                continue
            # Jump to a later lettered question, skipping intermediates.
            if go and go in by_letter:
                jump_idx = letters.index(go)
                if jump_idx > idx:
                    dfs(jump_idx, path + [(q.id, o.label)])
                    continue
            # Continue into the next lettered question.
            if go and idx + 1 < len(letters) and go == letters[idx + 1]:
                dfs(idx + 1, path + [(q.id, o.label)])

    dfs(0, [])

    uniq: List[List[Tuple[str, str]]] = []
    seen: Set[tuple] = set()
    for p in paths:
        key = tuple(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _rule_from_paths(
    paths: List[List[Tuple[str, str]]],
    *,
    raw: Optional[str] = None,
) -> Optional[VisibilityRule]:
    if not paths:
        return None
    # Empty path → always unlocked (no rule).
    if any(len(p) == 0 for p in paths):
        return None

    def path_rule(path: List[Tuple[str, str]]) -> VisibilityRule:
        if len(path) == 1:
            qid, lab = path[0]
            return VisibilityRule(
                field=qid, equals=lab, source="skip_logic", raw=raw
            )
        return VisibilityRule(
            all=[RuleClause(field=qid, equals=lab) for qid, lab in path],
            source="skip_logic",
            raw=raw,
        )

    if len(paths) == 1:
        return path_rule(paths[0])
    return VisibilityRule(
        any=[path_rule(p) for p in paths],
        source="skip_logic",
        raw=raw,
    )


def annotate_skip_cascade(
    spec: FormSpec,
    mappings: Dict[str, dict],
    fields_info: List[dict],
    label_data: Dict[str, dict],
    *,
    widget_key=None,
) -> Tuple[FormSpec, Dict[str, dict]]:
    """Attach unlock rules for lettered questions and section-letter fields."""
    if widget_key is None:
        def widget_key(f: dict) -> str:
            return f["name"]

    lettered = _lettered_questions(spec)
    if len(lettered) < 2 and not any(
        _parse_skip_branch(o.skip_logic)[0] or _parse_skip_branch(o.skip_logic)[1]
        for q in spec.questions
        for o in q.options
    ):
        return spec, mappings

    # Targets mentioned in skip text (D may have no Yes/No question).
    targets: Set[str] = {let for let, _ in lettered}
    for q in spec.questions:
        for o in q.options:
            skipped, go = _parse_skip_branch(o.skip_logic)
            targets |= skipped
            if go:
                targets.add(go)

    raw_blob = " · ".join(
        dict.fromkeys(
            o.skip_logic
            for q in spec.questions
            for o in q.options
            if o.skip_logic
        )
    )

    # Question unlock: lettered questions after the first.
    for let, q in lettered:
        if let == lettered[0][0]:
            continue
        paths = _paths_reaching(lettered, let)
        rule = _rule_from_paths(paths, raw=raw_blob or q.skip_logic)
        if rule is not None:
            q.rule = rule

    def _base_name(name: str) -> str:
        # Drug Name_2 → Drug Name; Dates of Therapy3 → Dates of Therapy
        return re.sub(r"(_\d+|\d+)$", "", name).rstrip()

    # Form-specific tables unlock when skip logic reaches their letter (D. …).
    for table in getattr(spec, "tables", None) or []:
        let = _item_letter(table.subsection, table.title)
        if not let:
            continue
        paths = _paths_reaching(lettered, let)
        rule = _rule_from_paths(paths, raw=raw_blob)
        if rule is not None:
            table.rule = rule

    table_keys = getattr(spec, "table_field_keys", None) or set()

    # Remaining lettered *data* blanks not owned by a table (How Long uses linked_field).
    for let in sorted(targets):
        paths = _paths_reaching(lettered, let)
        rule = _rule_from_paths(paths, raw=raw_blob)
        if rule is None:
            continue

        section_keys: Set[str] = set()
        bases: Set[str] = set()
        for f in fields_info:
            name = str(f.get("name") or "")
            if not name or field_kind(f) != DATA or name in table_keys:
                continue
            e = label_data.get(widget_key(f)) or label_data.get(name) or {}
            if _item_letter(e.get("subsection")) == let:
                section_keys.add(map_field_key(f))
                bases.add(_base_name(name))
        if bases:
            for f in fields_info:
                name = str(f.get("name") or "")
                if (
                    name
                    and name not in table_keys
                    and field_kind(f) == DATA
                    and _base_name(name) in bases
                ):
                    section_keys.add(map_field_key(f))

        for key in section_keys:
            entry = mappings.get(key)
            if not isinstance(entry, dict):
                continue
            existing = entry.get("rule") or {}
            if isinstance(existing, dict) and existing.get("source") == "linked_field":
                continue
            entry = dict(entry)
            entry["rule"] = rule.model_dump(mode="json")
            entry.pop("skip_logic", None)
            mappings[key] = entry

    # Drop stray skip_logic rules previously stamped onto choice widgets.
    for key, entry in list(mappings.items()):
        if not isinstance(entry, dict):
            continue
        if "::" in key or entry.get("canonical") in (None, "other"):
            r = entry.get("rule") or {}
            if isinstance(r, dict) and r.get("source") == "skip_logic":
                entry = dict(entry)
                entry.pop("rule", None)
                mappings[key] = entry

    return spec, mappings


def annotate_mapping_rules(
    mappings: Dict[str, dict],
    fields_info: List[dict],
    label_data: Dict[str, dict],
    spec: FormSpec,
    *,
    widget_key=None,
) -> Dict[str, dict]:
    """Add linked_field / conditional / skip_logic / rule onto mapping entries.

    Rules are keyed for the webform: ``field`` is the form-spec **question id**
    (Guided Fill looks up answers by that id). Dependent controls stay visible
    but locked until the controlling answer matches ``equals``.
    """
    if widget_key is None:
        def widget_key(f: dict) -> str:
            return f["name"]

    links = link_inline_blanks(fields_info)
    option_index: Dict[str, str] = {}
    for q in spec.questions:
        for o in q.options:
            option_index.setdefault(_norm_question(o.label).lower(), o.field)

    out: Dict[str, dict] = {}
    for f in fields_info:
        name = str(f.get("name") or "")
        if not name:
            continue
        key = map_field_key(f)
        entry = mappings.get(key) or mappings.get(name)
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        e = label_data.get(widget_key(f)) or label_data.get(name) or {}

        linked = links.get(name) or e.get("linked_field")
        conditional = e.get("conditional")
        skip = e.get("skip_logic")
        if linked:
            entry["linked_field"] = str(linked)
        if conditional:
            entry["conditional"] = str(conditional)
        if skip:
            entry["skip_logic"] = str(skip)

        rule: Optional[VisibilityRule] = None
        if linked:
            ctrl = _controller_for_linked(str(linked), spec)
            if ctrl:
                qid, equals = ctrl
                rule = VisibilityRule(
                    field=qid,
                    equals=equals,
                    source="linked_field",
                    raw=str(conditional) if conditional else None,
                )
        if rule is None and conditional:
            parsed = _parse_conditional(str(conditional), option_index)
            if parsed:
                # Remap option field → question id for the webform.
                ctrl = None
                for q in spec.questions:
                    if any(o.field == parsed.field or o.acro_field == parsed.field for o in q.options):
                        equals = parsed.equals
                        if equals is None:
                            for opt in q.options:
                                if _yes_like(opt.label):
                                    equals = opt.label
                                    break
                        ctrl = VisibilityRule(
                            field=q.id,
                            equals=equals,
                            source="conditional_text",
                            raw=str(conditional),
                        )
                        break
                rule = ctrl or parsed

        if rule is not None:
            entry["rule"] = rule.model_dump(mode="json")
        out[key] = entry

    # Keep any mapping keys we didn't see in fields_info (shouldn't happen).
    for k, v in mappings.items():
        if k not in out and isinstance(v, dict):
            out[k] = v
    return out


def apply_intake_annotations(
    mappings: Dict[str, dict],
    fields_info: List[dict],
    label_data: Dict[str, dict],
    spec: FormSpec,
    *,
    widget_key=None,
) -> Tuple[Dict[str, dict], FormSpec]:
    """Annotate both halves after a build/rebuild. Returns (mappings, spec).

    Also strips checkbox/branch rows from the canonical map — those live only
    in the FormSpec (Questions & checkboxes tab).
    """
    mappings, dropped = prune_form_specific_mappings(mappings, fields_info)
    if dropped:
        print(f"  🧹  Pruned {dropped} form-specific (choice/narrative/sig) "
              f"rows from canonical map → FormSpec only")

    # Section-aware patient ↔ prescriber fix for maps built on bare labels.
    mappings, sec_n = remap_mappings_by_section(
        mappings, fields_info, label_data, widget_key=widget_key
    )
    if sec_n:
        print(f"  🔀  Remapped {sec_n} field(s) patient↔prescriber via section")

    # Rebuild grids from extract column metadata (numeric /T names + orphans).
    from .form_spec_builder import build_form_tables

    taken = {q.id for q in spec.questions}
    taken |= {lt.field for lt in (spec.long_text or [])}
    built = build_form_tables(
        fields_info, label_data, widget_key=widget_key, taken=taken
    )
    if built:
        spec.tables = built

    # Drop section-title widgets authored as fillable text boxes.
    title_drop = 0
    cleaned = {}
    for k, v in mappings.items():
        if is_section_title_field(k, ""):
            title_drop += 1
            continue
        if isinstance(v, dict) and v.get("skip_logic") and not v.get("linked_field"):
            # Branch skip text belongs on FormSpec options, not data rows.
            v = dict(v)
            v.pop("skip_logic", None)
        cleaned[k] = v
    mappings = cleaned
    if title_drop:
        print(f"  🧹  Dropped {title_drop} section-title field(s) from canonical map")

    spec = annotate_form_spec_options(
        spec, fields_info, label_data, widget_key=widget_key
    )
    mappings = annotate_mapping_rules(
        mappings, fields_info, label_data, spec, widget_key=widget_key
    )
    # Cascade runs after prune so D-table data fields keep unlock rules, while
    # A/B/C checkboxes are already gone from the canonical half.
    spec, mappings = annotate_skip_cascade(
        spec, mappings, fields_info, label_data, widget_key=widget_key
    )
    # Second prune in case annotate_mapping_rules re-touched a choice key.
    mappings, _ = prune_form_specific_mappings(mappings, fields_info)

    # Table cells are form-specific — never keep them on the canonical map.
    table_keys = spec.table_field_keys
    if table_keys:
        before = len(mappings)
        mappings = {k: v for k, v in mappings.items() if k not in table_keys}
        dropped_t = before - len(mappings)
        if dropped_t:
            print(f"  🧹  Pruned {dropped_t} table-cell rows from canonical map "
                  f"→ FormSpec.tables")

    # Signature lines (+ companion dates) live only on FormSpec.
    from .form_spec_builder import signature_field_keys

    sig_keys = signature_field_keys(spec)
    if sig_keys:
        before = len(mappings)
        mappings = {k: v for k, v in mappings.items() if k not in sig_keys}
        dropped_s = before - len(mappings)
        if dropped_s:
            print(f"  🧹  Pruned {dropped_s} signature/date row(s) from canonical map "
                  f"→ FormSpec.signatures")

    # Leftovers (unmapped / other / non-catalog) → Guided Fill extras.
    from .form_spec_builder import build_form_extras

    spec.extras = build_form_extras(
        fields_info, label_data, mappings, spec, widget_key=widget_key
    )
    spec.extras_version = 1
    if spec.extras:
        print(f"  📎  {len(spec.extras)} leftover field(s) → FormSpec.extras "
              f"(Guided Fill Additional fields)")

    return mappings, spec


def sync_field_kinds(data: dict, fields_info: List[dict]) -> dict:
    """Stamp ``field_kinds`` on a cache payload so Mapping Review hides choices."""
    data = dict(data)
    data["field_kinds"] = field_kinds_for(fields_info)
    return data
