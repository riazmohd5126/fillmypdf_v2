#!/usr/bin/env python3
"""
pa_vision_mapper.py — resolve the fields the name-matcher couldn't, by SHOWING
a vision model the rendered page and the field's position so it can read the
printed label next to it.

Why this exists: many PA PDFs have meaningless field names (undefined_3, Text1,
Check Box9). The same name means different things in different forms, so this
MUST work per-form, per-page using the widget's position — not the global name
map. It reads unresolved fields straight from pa_forms.db (which is per-form).

Cache-first: only fields the name-matcher left as low/none confidence are sent
to the model. Already-confident fields cost nothing. Only pages that actually
contain an unresolved field get rendered + sent.

Supported providers:
  gemini    (default) — uses the OpenAI-compatible Gemini endpoint with the
                        openai SDK, model gemini-2.5-flash. Set GEMINI_API_KEY.
  anthropic           — original Claude backend. Set ANTHROPIC_API_KEY.

PIPELINE FIT: this is the offline, NO-PHI pass — it runs on BLANK forms to
build the mapping. Production runtime (real patient data) uses self-hosted Qwen
so PHI stays on-prem.

USAGE:
  pip install "pypdf[crypto]" pymupdf openai
  export GEMINI_API_KEY=...
  python3 pa_vision_mapper.py --db schema_out/pa_forms.db --limit 5   # test first!
  python3 pa_vision_mapper.py --db schema_out/pa_forms.db             # full run
  python3 pa_vision_mapper.py --db schema_out/pa_forms.db --dry-run   # no API calls
  python3 pa_vision_mapper.py --db schema_out/pa_forms.db --provider anthropic
"""

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Provider defaults and pricing  ($/M tokens  input, output)
# ---------------------------------------------------------------------------

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL    = "gemini-2.5-flash"
CLAUDE_MODEL    = "claude-sonnet-4-6"

PRICE = {
    # Gemini 2.5 Flash (approx May 2026 pricing)
    "gemini-2.5-flash":     (0.15, 0.60),
    "gemini-2.5-pro":       (1.25, 5.00),
    # Claude
    "claude-sonnet-4-6":    (3.0,  15.0),
    "claude-haiku-4-5":     (1.0,   5.0),
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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
    """form_id -> {file, fields} for low/none-confidence fields."""
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


def _user_text(fields) -> str:
    field_lines = "\n".join(f"- {n} : {box}" for n, box in fields)
    return (f"Fields to map on this page (name : pixel box):\n{field_lines}\n"
            "Return JSON only.")


# ---------------------------------------------------------------------------
# Gemini backend  (openai SDK → Gemini OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------

def init_gemini(api_key: str):
    """Return an openai.OpenAI client pointed at the Gemini endpoint."""
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL)


def _retry_delay_from_error(exc) -> float:
    """Parse 'please retry in Xs' from a rate-limit error message, else return 0."""
    try:
        msg = str(exc)
        m = re.search(r"retry[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*s", msg, re.IGNORECASE)
        if m:
            return float(m.group(1)) + 2.0   # add 2s buffer
    except Exception:
        pass
    return 0.0


def call_gemini(client, model: str, system: str,
                png_bytes: bytes, fields: list, dry_run=False,
                rpm_limit: int = 5, max_retries: int = 5):
    """Call Gemini vision via the OpenAI-compatible endpoint.

    Handles free-tier rate limits (default 5 RPM) with automatic retry/backoff.
    Returns (parsed_results, (input_tokens, output_tokens)).
    """
    if dry_run:
        print(f"    [dry-run] would send page image + {len(fields)} fields to Gemini")
        return [], (0, 0)

    b64 = base64.standard_b64encode(png_bytes).decode()
    data_uri = f"data:image/png;base64,{b64}"

    # Minimum inter-request gap to stay inside the RPM limit
    min_gap = 60.0 / rpm_limit + 1.0   # e.g. 5 RPM → 13s gap

    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_tokens=2000,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text",      "text": _user_text(fields)},
                    ]},
                ],
            )
            raw = (resp.choices[0].message.content or "").strip()
            usage = resp.usage
            tok_in  = getattr(usage, "prompt_tokens",    0) if usage else 0
            tok_out = getattr(usage, "completion_tokens", 0) if usage else 0
            # Throttle: sleep before the next call to respect RPM
            time.sleep(min_gap)
            return parse_json(raw), (tok_in, tok_out)
        except Exception as exc:
            wait = _retry_delay_from_error(exc)
            if wait == 0.0:
                # Exponential backoff for non-rate-limit errors (503, etc.)
                wait = min(60.0, 10.0 * (2 ** attempt))
            if attempt < max_retries - 1:
                print(f"      [retry {attempt+1}/{max_retries-1} after {wait:.0f}s: "
                      f"{type(exc).__name__}]")
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------------------
# Anthropic (Claude) backend  — original implementation, kept for compatibility
# ---------------------------------------------------------------------------

def init_anthropic():
    try:
        import anthropic as _ant
        return _ant.Anthropic(timeout=120.0, max_retries=2)
    except Exception as e:
        print(f"!! could not init Anthropic client: {e}")
        sys.exit(1)


def call_claude(client, model: str, system: str,
                png_bytes: bytes, fields: list, dry_run=False):
    if dry_run:
        print(f"    [dry-run] would send page image + {len(fields)} fields to Claude")
        return [], (0, 0)
    b64 = base64.standard_b64encode(png_bytes).decode()
    msg = client.messages.create(
        model=model, max_tokens=2000,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": "image/png", "data": b64}},
            {"type": "text", "text": _user_text(fields)}]}])
    out = "".join(b.text for b in msg.content if b.type == "text")
    usage = (msg.usage.input_tokens, msg.usage.output_tokens)
    return parse_json(out), usage


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Resolve unmapped PA form fields using a vision model.")
    ap.add_argument("--db",       default="schema_out/pa_forms.db")
    ap.add_argument("--provider", default="gemini",
                    choices=["gemini", "anthropic"],
                    help="Vision provider: gemini (default) or anthropic")
    ap.add_argument("--model",    default="",
                    help="Override model name (default: gemini-2.5-flash or claude-sonnet-4-6)")
    ap.add_argument("--api-key",  default="",
                    help="API key (falls back to GEMINI_API_KEY / ANTHROPIC_API_KEY env)")
    ap.add_argument("--limit",    type=int, default=0,
                    help="Process only N forms (0 = all)")
    ap.add_argument("--dpi",      type=int, default=120)
    ap.add_argument("--rpm",      type=int, default=5,
                    help="Requests per minute quota (free tier=5, paid=60+)")
    ap.add_argument("--dry-run",  action="store_true")
    args = ap.parse_args()

    # Resolve model default
    if not args.model:
        args.model = GEMINI_MODEL if args.provider == "gemini" else CLAUDE_MODEL

    # Open DB
    db = sqlite3.connect(args.db)
    cols = [r[1] for r in db.execute("PRAGMA table_info(form_fields)")]
    if "source" not in cols:
        db.execute("ALTER TABLE form_fields ADD COLUMN source TEXT DEFAULT 'name'")
        db.commit()

    schema = load_schema(db)
    valid  = {s[0] for s in schema} | {"other"}
    system = schema_prompt(schema)

    # Init client
    client = None
    if not args.dry_run:
        if args.provider == "gemini":
            api_key = (args.api_key or "").strip() or os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                print("!! Set GEMINI_API_KEY env var or pass --api-key")
                sys.exit(1)
            client = init_gemini(api_key)
            print(f"Provider: Gemini  model={args.model}")
        else:
            api_key = (args.api_key or "").strip() or os.getenv("ANTHROPIC_API_KEY", "")
            if not api_key:
                print("!! Set ANTHROPIC_API_KEY env var or pass --api-key")
                sys.exit(1)
            os.environ["ANTHROPIC_API_KEY"] = api_key
            client = init_anthropic()
            print(f"Provider: Anthropic  model={args.model}")

    call_fn = call_gemini if args.provider == "gemini" else call_claude

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
                kw = {"rpm_limit": args.rpm} if args.provider == "gemini" else {}
                results, (ui, uo) = call_fn(
                    client, args.model, system, png, boxed, args.dry_run, **kw)
            except Exception as e:
                print(f"      [page {pno} skipped: {type(e).__name__}: {e}]")
                continue
            tot_in += ui; tot_out += uo; pages_sent += 1
            for r in results:
                fld   = r.get("field");  canon = r.get("canonical")
                conf  = r.get("confidence", "medium")
                if fld in want and canon in valid and canon != "other":
                    db.execute(
                        "UPDATE form_fields SET canonical_field=?, confidence=?, "
                        "source='vision' WHERE form_id=? AND raw_name=?",
                        (canon, conf, fid, fld))
                    resolved += 1
        db.commit()

    pin, pout = PRICE.get(args.model, (3.0, 15.0))
    cost = tot_in / 1e6 * pin + tot_out / 1e6 * pout
    print(f"\n{'='*52}\nVISION MAPPING DONE\n{'='*52}")
    print(f"  provider:            {args.provider} / {args.model}")
    print(f"  pages sent to model: {pages_sent}")
    print(f"  fields resolved:     {resolved}")
    if not args.dry_run:
        print(f"  tokens: {tot_in:,} in / {tot_out:,} out")
        print(f"  est. cost: ${cost:.4f}  (standard rate)")
    print(f"  DB updated: {args.db} (form_fields.source='vision' on resolved rows)")


if __name__ == "__main__":
    main()
