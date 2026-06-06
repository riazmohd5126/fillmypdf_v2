#!/usr/bin/env python3
"""
A/B Test: Gemini vs Local Qwen (or any two Ollama models)
==========================================================
Runs the SAME template + SAME patient record through two different LLM
providers back-to-back and prints a side-by-side comparison of:

  - fields detected / filled
  - avg confidence score
  - per-field mapped values (so you can spot wrong answers)
  - latency

Usage
-----
    # Compare two local Ollama models (no cloud key needed):
    python3 scripts/ab_test_providers.py \
        --api-key fmp_live_XXX \
        --provider-a local --model-a llama3.1:latest \
        --provider-b local --model-b qwen2.5:3b-instruct \
        --template pa_linzess_molina_tx

    # Compare Gemini (cloud) vs local Qwen:
    python3 scripts/ab_test_providers.py \
        --api-key fmp_live_XXX \
        --provider-a gemini --gemini-key AIza... \
        --provider-b local --model-b qwen2.5:3b-instruct \
        --template pa_linzess_molina_tx

    # Override the test patient record:
    python3 scripts/ab_test_providers.py ... --data '{"patient_name":"Jane Doe","dob":"1985-03-22"}'

Required: FillMyPDF API running on http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional
import urllib.request
import urllib.error


# ── Default test record (typical prior-auth patient + prescriber data) ──────
DEFAULT_TEST_DATA = {
    "patient_name": "Jane Doe",
    "date_of_birth": "1985-03-22",
    "patient_address": "123 Main Street, Boston MA 02101",
    "patient_phone": "617-555-0100",
    "insurance_id": "INS-987654321",
    "group_number": "GRP-001",
    "physician_name": "Dr. John Smith",
    "physician_npi": "1234567890",
    "physician_phone": "617-555-0200",
    "diagnosis_code": "K58.0",
    "drug_name": "Linzess",
    "drug_dose": "290mcg",
    "quantity": "30 capsules",
    "days_supply": "30",
    "date_of_service": "2026-06-06",
}

API_BASE = "http://localhost:8000"


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _post_form(url: str, api_key: str, fields: dict) -> tuple[dict, float]:
    """POST multipart form data, return (parsed_json, elapsed_seconds)."""
    boundary = "----FillMyPDFABTest"
    parts = []
    for key, val in fields.items():
        if val is None:
            continue
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{val}\r\n"
        )
    body = ("".join(parts) + f"--{boundary}--\r\n").encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "X-API-Key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            elapsed = time.perf_counter() - t0
            return json.loads(resp.read().decode()), elapsed
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - t0
        body_text = e.read().decode(errors="replace")
        return {"error": f"HTTP {e.code}: {body_text[:300]}"}, elapsed


def _get(url: str, api_key: str) -> dict:
    req = urllib.request.Request(url, headers={"X-API-Key": api_key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


# ── Core A/B runner ──────────────────────────────────────────────────────────

def run_fill(
    *,
    api_key: str,
    template_id: str,
    record: dict,
    provider: str,
    model: Optional[str],
    gemini_key: Optional[str],
    use_coords: bool,
    run_id: str = "",
) -> dict:
    """
    Call POST /api/v1/templates/{id}/fill and return the result dict
    augmented with latency and the provider label.

    run_id is appended as a dummy field to bust the template mapping cache
    so each provider is evaluated independently without reusing the other's
    cached mappings.
    """
    # Add a unique sentinel field so each A/B run gets its own cache slot
    record_with_salt = {**record, "_ab_run": run_id}

    fields: dict = {
        "user_data": json.dumps(record_with_salt),
        "ai_provider": provider,
        "dpi": "200",
        "return_mappings": "true",
    }
    if provider == "gemini" and gemini_key:
        fields["ai_api_key"] = gemini_key
    if model:
        fields["ai_model"] = model

    url = f"{API_BASE}/api/v1/templates/{template_id}/fill"
    result, elapsed = _post_form(url, api_key, fields)
    result["_elapsed"] = round(elapsed, 2)
    result["_provider_label"] = f"{provider}/{model or 'default'}"
    return result


# ── Display helpers ──────────────────────────────────────────────────────────

W = 44  # column width for side-by-side display


def _col(text: str, width: int = W) -> str:
    s = str(text)
    return s[:width].ljust(width)


def _bar(score: float, width: int = 20) -> str:
    filled = round(score * width)
    return "█" * filled + "░" * (width - filled)


def print_comparison(a: dict, b: dict, record: dict) -> None:
    label_a = a.get("_provider_label", "Provider A")
    label_b = b.get("_provider_label", "Provider B")

    print()
    print("=" * (W * 2 + 3))
    print(f"  {'A/B COMPARISON':^{W * 2 + 1}}")
    print("=" * (W * 2 + 3))
    print(f"  {_col(label_a)}  {_col(label_b)}")
    print("-" * (W * 2 + 3))

    # Error check
    if "error" in a or "error" in b:
        if "error" in a:
            print(f"  ❌ Provider A error: {a['error']}")
        if "error" in b:
            print(f"  ❌ Provider B error: {b['error']}")
        return

    # Summary metrics
    def _m(r: dict) -> str:
        filled = r.get("fields_filled", 0)
        detected = r.get("fields_detected", 0)
        conf = r.get("avg_confidence")
        conf_str = f"{conf:.0%}" if conf is not None else "n/a"
        cache = " [cache]" if r.get("cache_hit") else ""
        return f"Filled {filled}/{detected}  conf={conf_str}  {r['_elapsed']}s{cache}"

    print(f"  {_col(_m(a))}  {_col(_m(b))}")
    print()

    # Confidence bar
    conf_a = a.get("avg_confidence") or 0
    conf_b = b.get("avg_confidence") or 0
    print(f"  Confidence:  [{_bar(conf_a)}] {conf_a:.0%}  vs  [{_bar(conf_b)}] {conf_b:.0%}")
    print()

    # Per-field comparison
    mappings_a: dict = a.get("mappings", {})
    mappings_b: dict = b.get("mappings", {})
    conf_detail_a: dict = a.get("confidence", {})
    conf_detail_b: dict = b.get("confidence", {})

    all_fields = sorted(set(list(mappings_a.keys()) + list(mappings_b.keys())))
    if not all_fields:
        print("  (no per-field mapping detail returned)")
        return

    print(f"  {'FIELD':<30} {'── Provider A ──':<{W}}  {'── Provider B ──':<{W}}")
    print(f"  {'':<30} {'value':<34} {'conf':>4}  {'value':<34} {'conf':>4}")
    print("  " + "-" * (30 + W * 2 + 10))

    match = same = diff = 0
    for field in all_fields:
        val_a = mappings_a.get(field, "")
        val_b = mappings_b.get(field, "")
        c_a = conf_detail_a.get(field)
        c_b = conf_detail_b.get(field)
        ca_str = f"{c_a:.0%}" if c_a is not None else "   "
        cb_str = f"{c_b:.0%}" if c_b is not None else "   "

        indicator = "✓" if val_a and val_b and val_a == val_b else ("≠" if val_a and val_b else "·")
        if val_a and val_b:
            match += 1
            if val_a == val_b:
                same += 1
            else:
                diff += 1

        val_a_s = (val_a or "—")[:33]
        val_b_s = (val_b or "—")[:33]
        print(f"  {indicator} {field:<28} {val_a_s:<34} {ca_str:>4}  {val_b_s:<34} {cb_str:>4}")

    print()
    print(f"  Agreement on fields both filled: {same}/{match} same values, {diff} different")
    print()

    # Verdict
    winner = None
    if conf_a > conf_b + 0.05:
        winner = f"A ({label_a}) — higher confidence"
    elif conf_b > conf_a + 0.05:
        winner = f"B ({label_b}) — higher confidence"
    elif a.get("fields_filled", 0) > b.get("fields_filled", 0):
        winner = f"A ({label_a}) — more fields filled"
    elif b.get("fields_filled", 0) > a.get("fields_filled", 0):
        winner = f"B ({label_b}) — more fields filled"
    else:
        winner = "Tie — same fields filled and similar confidence"

    speed_winner = label_a if a["_elapsed"] < b["_elapsed"] else label_b
    print(f"  📊 Accuracy edge:  {winner}")
    print(f"  ⚡ Speed edge:     {speed_winner}  ({a['_elapsed']}s vs {b['_elapsed']}s)")
    print()

    # Recommendation
    if conf_a > 0.85 or conf_b > 0.85:
        print("  ✅ At least one provider reached >85% avg confidence — usable for PA fills.")
    else:
        print("  ⚠️  Both providers below 85% avg confidence — check field labels or increase model size.")
    print("=" * (W * 2 + 3))


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="A/B test two LLM providers on a FillMyPDF template.")
    p.add_argument("--api-key", required=True, help="FillMyPDF API key (X-API-Key)")
    p.add_argument("--template", default="pa_linzess_molina_tx",
                   help="Template ID (default: pa_linzess_molina_tx)")
    p.add_argument("--data", default=None,
                   help="JSON string of patient record (uses built-in PA sample if omitted)")

    g_a = p.add_argument_group("Provider A")
    g_a.add_argument("--provider-a", default="local", choices=["gemini", "local"],
                     help="Provider A type (default: local)")
    g_a.add_argument("--model-a", default="llama3.1:latest",
                     help="Model for provider A (default: llama3.1:latest)")

    g_b = p.add_argument_group("Provider B")
    g_b.add_argument("--provider-b", default="local", choices=["gemini", "local"],
                     help="Provider B type (default: local)")
    g_b.add_argument("--model-b", default="qwen2.5:3b-instruct",
                     help="Model for provider B (default: qwen2.5:3b-instruct)")

    p.add_argument("--gemini-key", default=None, help="Gemini API key (required if provider=gemini)")
    p.add_argument("--coords", action="store_true",
                   help="Enable AI_USE_COORDINATES for both runs (tests coordinate enhancement)")
    p.add_argument("--api-base", default="http://localhost:8000",
                   help="FillMyPDF API base URL (default: http://localhost:8000)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    global API_BASE
    API_BASE = args.api_base.rstrip("/")

    record = json.loads(args.data) if args.data else DEFAULT_TEST_DATA

    print(f"\n🧪 A/B Test: {args.provider_a}/{args.model_a}  vs  {args.provider_b}/{args.model_b}")
    print(f"   Template:  {args.template}")
    print(f"   Record:    {len(record)} fields  ({', '.join(list(record.keys())[:4])}…)")
    if args.coords:
        print("   Coords:    AI_USE_COORDINATES=true (needs server restart with that env var)")
    print()

    # Verify template exists
    try:
        _get(f"{API_BASE}/api/v1/templates/{args.template}", args.api_key)
    except Exception as e:
        print(f"❌ Cannot reach template '{args.template}': {e}")
        print("   Is the FillMyPDF API running? Try: python3 -m uvicorn fillmypdf.main:app --port 8000")
        sys.exit(1)

    run_ts = str(int(time.time()))

    print(f"▶  Running Provider A ({args.provider_a}/{args.model_a})…", flush=True)
    result_a = run_fill(
        api_key=args.api_key,
        template_id=args.template,
        record=record,
        provider=args.provider_a,
        model=args.model_a,
        gemini_key=args.gemini_key,
        use_coords=args.coords,
        run_id=f"a_{run_ts}",
    )
    status_a = "✓" if "error" not in result_a else "✗"
    print(f"   {status_a} done in {result_a['_elapsed']}s")

    print(f"▶  Running Provider B ({args.provider_b}/{args.model_b})…", flush=True)
    result_b = run_fill(
        api_key=args.api_key,
        template_id=args.template,
        record=record,
        provider=args.provider_b,
        model=args.model_b,
        gemini_key=args.gemini_key,
        use_coords=args.coords,
        run_id=f"b_{run_ts}",
    )
    status_b = "✓" if "error" not in result_b else "✗"
    print(f"   {status_b} done in {result_b['_elapsed']}s")

    print_comparison(result_a, result_b, record)


if __name__ == "__main__":
    main()
