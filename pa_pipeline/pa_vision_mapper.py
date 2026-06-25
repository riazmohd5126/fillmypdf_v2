#!/usr/bin/env python3
"""
pa_vision_mapper.py — resolve the fields the name-matcher couldn't, by SHOWING
Claude the rendered page and the field's position so it can read the printed
label next to it.

Why this exists: many PA PDFs have meaningless field names (undefined_3, Text1,
Check Box9). The same name means different things in different forms, so this
MUST work per-form, per-page using the widget's position — not the global name
map. It reads unresolved fields straight from pa_forms.db (which is per-form).

Cache-first: only fields the name-matcher left as low/none confidence are sent
to the model. Already-confident fields cost nothing. Only pages that actually
contain an unresolved field get rendered + sent.

PIPELINE FIT: this is the offline, NO-PHI pass — it runs on BLANK forms to build
the mapping. Production runtime (real patient data) uses self-hosted Qwen so PHI
stays on-prem. Same prompt, different --model.

USAGE:
  pip install "pypdf[crypto]" pymupdf anthropic
  export ANTHROPIC_API_KEY=...
  python3 pa_vision_mapper.py --db schema_out/pa_forms.db --limit 5   # test first!
  python3 pa_vision_mapper.py --db schema_out/pa_forms.db             # full run
  python3 pa_vision_mapper.py --db schema_out/pa_forms.db --dry-run   # no API calls
"""

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

MODEL = "claude-sonnet-4-6"
PRICE = {"claude-sonnet-4-6": (3.0, 15.0),  # $/Mtok input, output
         "claude-haiku-4-5": (1.0, 5.0)}


def load_schema(db) -> list[tuple]:
    return [(r[0], r[1], r[2]) for r in
            db.execute("SELECT name, entity, critical FROM canonical_fields")]


def schema_prompt(schema) -> str:
    lines = ["You map PDF form fields to a FIXED canonical schema for prior-",
             "authorization forms. Canonical fields (name — entity):"]
    for name, entity, crit in schema:
        lines.append(f"  {name} ({entity}){' *critical*' if crit else ''}")
    lines += [
        "",
        "I give you a rendered form page and a list of fields with their pixel",
        "bounding boxes [x0,y0,x1,y1] on that image. For EACH field, read the",
        "printed label nearest its box and map it to exactly one canonical name",
        "above, or \"other\" if none fits.",
        "Respond with ONLY a JSON array, no prose:",
        '[{"field":"<exact name>","canonical":"<schema name or other>",'
        '"confidence":"high|medium|low"}]',
    ]
    return "\n".join(lines)


def unresolved_by_form(db):
    """form_id -> (file, [field_name,...]) for low/none-confidence fields."""
    out = defaultdict(lambda: {"file": None, "fields": []})
    q = ("SELECT f.form_id, f.file, ff.raw_name FROM form_fields ff "
         "JOIN forms f ON f.form_id = ff.form_id "
         "WHERE ff.confidence IN ('low','none')")
    for fid, file, raw in db.execute(q):
        out[fid]["file"] = file
        out[fid]["fields"].append(raw)
    return out


def render_and_locate(pdf_path: str, want: set, dpi=120):
    """Return {page_no: (png_bytes, [(field_name, rect_px), ...])} for pages
    that contain a wanted field."""
    import fitz
    doc = fitz.open(pdf_path)
    scale = dpi / 72.0
    pages = {}
    for pno in range(doc.page_count):
        page = doc[pno]
        widgets = list(page.widgets() or [])
        hits = [(w.field_name, w.rect) for w in widgets
                if w.field_name in want]
        if not hits:
            continue
        pix = page.get_pixmap(dpi=dpi)
        png = pix.tobytes("png")
        boxed = [(name, [round(c * scale, 1) for c in (r.x0, r.y0, r.x1, r.y1)])
                 for name, r in hits]
        pages[pno] = (png, boxed)
    doc.close()
    return pages


def parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        return json.loads(m.group(0)) if m else []


def call_claude(client, model, system, png_bytes, fields, dry_run=False):
    field_lines = "\n".join(f"- {n} : {box}" for n, box in fields)
    user_text = (f"Fields to map on this page (name : pixel box):\n{field_lines}\n"
                 "Return JSON only.")
    if dry_run:
        print(f"    [dry-run] would send page image + {len(fields)} fields")
        return [], (0, 0)
    b64 = base64.standard_b64encode(png_bytes).decode()
    msg = client.messages.create(
        model=model, max_tokens=2000,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],  # cache the schema
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": "image/png", "data": b64}},
            {"type": "text", "text": user_text}]}])
    out = "".join(b.text for b in msg.content if b.type == "text")
    usage = (msg.usage.input_tokens, msg.usage.output_tokens)
    return parse_json(out), usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="schema_out/pa_forms.db")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--limit", type=int, default=0, help="process only N forms (test)")
    ap.add_argument("--dpi", type=int, default=120)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    # add columns to record provenance, if not present
    cols = [r[1] for r in db.execute("PRAGMA table_info(form_fields)")]
    if "source" not in cols:
        db.execute("ALTER TABLE form_fields ADD COLUMN source TEXT DEFAULT 'name'")
        db.commit()

    schema = load_schema(db)
    valid = {s[0] for s in schema} | {"other"}
    system = schema_prompt(schema)

    client = None
    if not args.dry_run:
        try:
            import anthropic
            client = anthropic.Anthropic(timeout=120.0, max_retries=2)  # don't hang forever
        except Exception as e:
            print(f"!! could not init Anthropic client: {e}")
            sys.exit(1)
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("!! set ANTHROPIC_API_KEY"); sys.exit(1)

    work = unresolved_by_form(db)
    form_ids = list(work)[: args.limit] if args.limit else list(work)
    print(f"{len(work)} forms have unresolved fields; processing {len(form_ids)}")

    tot_in = tot_out = resolved = pages_sent = 0
    for i, fid in enumerate(form_ids, 1):
        file = work[fid]["file"]
        want = set(work[fid]["fields"])
        if not Path(file).exists():
            print(f"  [{i}] MISSING {file}"); continue
        try:
            pages = render_and_locate(file, want, args.dpi)
        except Exception as e:
            print(f"  [{i}] render err {type(e).__name__}: {file}"); continue
        print(f"  [{i}/{len(form_ids)}] {Path(file).name}: "
              f"{len(want)} unresolved over {len(pages)} page(s)")
        for pno, (png, boxed) in pages.items():
            try:
                results, (ui, uo) = call_claude(client, args.model, system, png, boxed, args.dry_run)
            except Exception as e:
                print(f"      [page {pno} skipped: {type(e).__name__}]")
                continue
            tot_in += ui; tot_out += uo; pages_sent += 1
            for r in results:
                fld = r.get("field"); canon = r.get("canonical")
                conf = r.get("confidence", "medium")
                if fld in want and canon in valid and canon != "other":
                    db.execute("UPDATE form_fields SET canonical_field=?, "
                               "confidence=?, source='vision' WHERE form_id=? AND raw_name=?",
                               (canon, conf, fid, fld))
                    resolved += 1
        db.commit()

    pin, pout = PRICE.get(args.model, (3.0, 15.0))
    cost = tot_in / 1e6 * pin + tot_out / 1e6 * pout
    print(f"\n{'='*52}\nVISION MAPPING DONE\n{'='*52}")
    print(f"  pages sent to model: {pages_sent}")
    print(f"  fields resolved:     {resolved}")
    if not args.dry_run:
        print(f"  tokens: {tot_in:,} in / {tot_out:,} out")
        print(f"  est. cost: ${cost:.2f}  (standard rate; batch API would halve it)")
    print(f"  DB updated: {args.db} (form_fields.source='vision' on resolved rows)")


if __name__ == "__main__":
    main()
