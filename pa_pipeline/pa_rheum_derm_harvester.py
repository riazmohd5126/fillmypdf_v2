#!/usr/bin/env python3
"""
pa_rheum_derm_harvester.py — harvest blank Prior Authorization PDFs for
RHEUMATOLOGY and DERMATOLOGY specifically, with VARIETY as the explicit
optimization target rather than raw volume.

Why a separate program instead of widening pa_form_harvester.py's DRUG_TERMS:
that harvester treats "specialty drug" as one flat bucket and just cross-
multiplies drugs x payers in nested loops. For two specialties that share a
LOT of molecules (Humira, Cosentyx, Otezla, Rinvoq, Stelara are all
rheum-AND-derm), a flat cross product either (a) buries the rare mechanics
under a wall of Humira-x-every-payer results, or (b) needs a hard query cap
that then exhausts the first few drugs alphabetically before ever reaching
the rest. Neither gives you a *diverse* test corpus.

This harvester instead:
  1. Classifies every drug by MECHANISM/CLASS (TNF, IL-17, IL-23, JAK, PDE4,
     topical, ...) and by CONDITION, via pa_rheum_derm_taxonomy.py.
  2. Builds three tiers of dork: condition-level (broadest net), drug-level
     (no payer — catches manufacturer/payer-agnostic forms), and drug x a
     ROTATING subset of payers (round-robin, not full cross product).
  3. INTERLEAVES the final query list round-robin across drug class /
     condition bucket, so even a small --max-queries cap samples every
     mechanism and both specialties instead of exhausting one axis first.
  4. Tags every downloaded file with (specialty, drug, drug_class, payer)
     at harvest time — this is ground truth, not a text-sniffing guess done
     after the fact — and lays files out by those axes on disk so
     pa_rheum_derm_profiler.py's coverage report is trivial.
  5. Supports --fill-gaps: read the existing index, and push queries for the
     thinnest (specialty, drug_class) cells to the FRONT of the interleaved
     order, so a follow-up run spends its query budget closing gaps first.

PIPELINE FIT: same offline, no-PHI harvest stage as pa_form_harvester.py —
this just aims it at two specialties with variety-first prioritization.
Downstream pa_profiler.py / pa_stratify.py / pa_schema_extractor.py all work
unmodified against pa_forms_rheum_derm/ (or point pa_rheum_derm_profiler.py
at it for the specialty-aware coverage report).

------------------------------------------------------------------ SETUP
  pip install requests "pypdf[crypto]" playwright
  playwright install chromium          # only needed for --payers / crawling

  Set ONE search-API key as an env var to enable --discover:
    export BRAVE_API_KEY=...           # api.search.brave.com (free tier)
    # or
    export SERPAPI_KEY=...
    # or
    export GOOGLE_CSE_KEY=...  GOOGLE_CSE_CX=...

------------------------------------------------------------------ RUN
  # Broad first pass, capped query budget, variety-first ordering:
  python3 pa_rheum_derm_harvester.py --discover --max-queries 80

  # Only rheumatology, wider payer rotation per drug:
  python3 pa_rheum_derm_harvester.py --discover --specialty rheum --payers-per-drug 8

  # Follow-up run that prioritizes whatever's still thin:
  python3 pa_rheum_derm_harvester.py --discover --fill-gaps --max-queries 60

  # Crawl the built-in specialty-pharmacy/payer form-library pages instead:
  python3 pa_rheum_derm_harvester.py --payers

  # See the query plan without spending API quota or downloading anything:
  python3 pa_rheum_derm_harvester.py --discover --dry-run
"""

import argparse
import csv
import hashlib
import itertools
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pa_rheum_derm_taxonomy import (  # noqa: E402
    ALL_DRUGS, DRUG_CLASSES, RHEUM_CONDITIONS, DERM_CONDITIONS,
    SPECIALTY_PAYERS, SPECIALTY_FORM_TERMS,
)

OUT = Path("pa_forms_rheum_derm")
OUT.mkdir(exist_ok=True)
INDEX = OUT / "_index.csv"
INDEX_COLS = ["file", "structure", "specialty", "drug", "drug_class", "condition", "payer", "source_url"]
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36")}

PAYER_PORTALS = [
    "https://info.caremark.com/dig/pa-forms",
    "https://www.uhcprovider.com/en/resource-library/provider-forms.html",
    "https://www.aetna.com/health-care-professionals/precertification/precertification-lists.html",
    "https://www.cigna.com/health-care-providers/coverage-and-claims/precertification",
    "https://provider.humana.com/coverage-claims/prior-authorizations",
    "https://www.covermymeds.com/main/prior-authorization-forms/",
]

# ---------------------------------------------------------------- ROBOTS
_robots_cache: dict[str, RobotFileParser] = {}
IGNORE_ROBOTS = False
FORMS_ONLY = False


def allowed(url: str) -> bool:
    if IGNORE_ROBOTS:
        return True
    host = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    rp = _robots_cache.get(host)
    if rp is None:
        rp = RobotFileParser()
        rp.set_url(urljoin(host, "/robots.txt"))
        try:
            rp.read()
        except Exception:
            rp = None
        _robots_cache[host] = rp
    return True if rp is None else rp.can_fetch(HEADERS["User-Agent"], url)


# ------------------------------------------------------- SEARCH PROVIDERS
def _provider():
    if os.getenv("BRAVE_API_KEY"):
        return "brave"
    if os.getenv("SERPAPI_KEY"):
        return "serpapi"
    if os.getenv("GOOGLE_CSE_KEY") and os.getenv("GOOGLE_CSE_CX"):
        return "cse"
    return None


def search(query: str, count: int = 20) -> list[str]:
    p = _provider()
    try:
        if p == "brave":
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"],
                         "Accept": "application/json"},
                params={"q": query, "count": min(count, 20)}, timeout=30)
            return [x["url"] for x in r.json().get("web", {}).get("results", [])]
        if p == "serpapi":
            r = requests.get("https://serpapi.com/search.json",
                             params={"engine": "google", "q": query,
                                     "num": count, "api_key": os.environ["SERPAPI_KEY"]},
                             timeout=30)
            return [x["link"] for x in r.json().get("organic_results", [])]
        if p == "cse":
            urls, start = [], 1
            while len(urls) < count:
                r = requests.get("https://www.googleapis.com/customsearch/v1",
                                 params={"key": os.environ["GOOGLE_CSE_KEY"],
                                         "cx": os.environ["GOOGLE_CSE_CX"],
                                         "q": query, "start": start}, timeout=30)
                items = r.json().get("items", [])
                if not items:
                    break
                urls += [i["link"] for i in items]
                start += 10
            return urls[:count]
    except Exception as e:
        print(f"  [search err {type(e).__name__}] {query}")
    return []


# ------------------------------------------------------------ DORK BUILD
def build_dork_records(specialty: str = "both", payers_per_drug: int = 5,
                        no_condition: bool = False, no_drug: bool = False,
                        no_payer: bool = False) -> list[dict]:
    """Return dork records: {query, specialty, drug, drug_class, condition, payer}.

    Three tiers, each tagged with ground-truth metadata (not inferred later):
      condition-level : broadest net, no drug/payer tied down
      drug-level       : drug name + generic PA phrase, no payer
      drug x payer     : drug paired with a ROTATING subset of payers so the
                          same few payers aren't repeated for every drug
    """
    records: list[dict] = []

    if not no_condition:
        conditions = ([("rheum", c) for c in RHEUM_CONDITIONS] if specialty == "rheum" else
                      [("derm", c) for c in DERM_CONDITIONS] if specialty == "derm" else
                      [("rheum", c) for c in RHEUM_CONDITIONS] + [("derm", c) for c in DERM_CONDITIONS])
        for spec, cond in conditions:
            for term in SPECIALTY_FORM_TERMS[:3]:
                records.append({"query": f'filetype:pdf "{cond}" {term}',
                                "specialty": spec, "drug": "", "drug_class": "",
                                "condition": cond, "payer": ""})

    drugs = {d: m for d, m in ALL_DRUGS.items()
             if specialty == "both" or m["specialty"] in (specialty, "both")}

    if not no_drug:
        for drug, meta in drugs.items():
            records.append({"query": f'filetype:pdf "prior authorization" {drug} form',
                            "specialty": meta["specialty"], "drug": drug,
                            "drug_class": meta["class"], "condition": meta["conditions"][0],
                            "payer": ""})

    if not no_payer:
        payer_cycle = itertools.cycle(SPECIALTY_PAYERS)
        for drug, meta in drugs.items():
            chosen = list(itertools.islice(payer_cycle, payers_per_drug))
            for payer in chosen:
                for term in SPECIALTY_FORM_TERMS[:2]:
                    records.append({"query": f'site:{payer} filetype:pdf {term} {drug}',
                                    "specialty": meta["specialty"], "drug": drug,
                                    "drug_class": meta["class"], "condition": meta["conditions"][0],
                                    "payer": payer})

    # de-dup identical queries while keeping first-seen metadata
    seen, out = set(), []
    for r in records:
        if r["query"] not in seen:
            seen.add(r["query"])
            out.append(r)
    return out


def _bucket_key(r: dict) -> str:
    return r["drug_class"] or f"condition:{r['specialty']}"


def interleave_variety(records: list[dict]) -> list[dict]:
    """Round-robin across (drug_class | condition-bucket) so a capped query
    budget still samples every mechanism/specialty rather than exhausting
    one bucket alphabetically before reaching the next."""
    buckets = defaultdict(list)
    for r in records:
        buckets[_bucket_key(r)].append(r)
    rows = list(buckets.values())
    return [r for group in itertools.zip_longest(*rows) for r in group if r is not None]


def existing_coverage(index_path: Path) -> dict[str, int]:
    """(specialty, drug_class) -> count of already-harvested forms, from a
    prior run's index CSV. Missing file => empty coverage (first run)."""
    counts: dict[str, int] = defaultdict(int)
    if not index_path.exists():
        return counts
    with index_path.open() as f:
        for row in csv.DictReader(f):
            key = f"{row.get('specialty', '')}|{row.get('drug_class', '')}"
            counts[key] += 1
    return counts


def prioritize_gaps(records: list[dict], counts: dict[str, int]) -> list[dict]:
    """Stable-sort so records whose (specialty, drug_class) cell has the
    fewest existing forms come first — spends a capped query budget closing
    the thinnest coverage first on a --fill-gaps re-run."""
    def key(r):
        return counts.get(f"{r['specialty']}|{r['drug_class']}", 0)
    return sorted(records, key=key)


# ------------------------------------------------------------- CLASSIFY
FORM_TOKENS = ["patient name", "date of birth", "dob", "member id", "fax",
               "prescriber", "npi", "signature", "diagnosis", "icd",
               "authorization request", "request form"]


def classify_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        try:
            root = reader.trailer["/Root"]
            if "/AcroForm" in root:
                if "/XFA" in root["/AcroForm"]:
                    return "xfa"
                fields = reader.get_fields()
                if fields:
                    return f"acroform({len(fields)})"
        except Exception:
            pass
        text = ""
        for pg in reader.pages[:3]:
            text += (pg.extract_text() or "").lower()
        score = sum(t in text for t in FORM_TOKENS)
        score += text.count("___") // 3
        score += text.count("☐")
        return "form-flat" if score >= 3 else "document"
    except Exception as e:
        return f"unreadable({type(e).__name__})"


def category(kind: str) -> str:
    if kind.startswith("acroform"):
        return "acroform"
    if kind == "xfa":
        return "xfa"
    if kind == "form-flat":
        return "form-flat"
    if kind == "document":
        return "document"
    return "unreadable"


def payer_from_url(url: str) -> str:
    labels = [l for l in urlparse(url).netloc.replace("www.", "").split(".") if l]
    if len(labels) >= 2:
        return labels[-2]
    return labels[0] if labels else "unknown"


def slugify(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower() or "unknown"


def infer_drug(text_blob: str) -> tuple[str, dict] | tuple[None, None]:
    """Best-effort tag for URLs found via crawling/enumeration (no query
    metadata to carry). Longest brand-name match wins to avoid a short name
    shadowing a more specific one."""
    hay = text_blob.lower()
    best = None
    for drug, meta in ALL_DRUGS.items():
        if drug.lower() in hay and (best is None or len(drug) > len(best[0])):
            best = (drug, meta)
    return best if best else (None, None)


# ------------------------------------------------------------- DOWNLOAD
def save(url: str, content: bytes, kind: str, meta: dict) -> Path | None:
    if content[:5] != b"%PDF-":
        return None
    h = hashlib.sha256(content).hexdigest()[:12]
    payer = meta.get("payer") or payer_from_url(url)
    specialty = meta.get("specialty") or "both"
    drug_class = meta.get("drug_class") or "unclassified"
    slug = slugify(Path(urlparse(url).path).stem)[:50]
    folder = OUT / category(kind) / specialty / slugify(drug_class) / slugify(payer)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slugify(payer)}_{slug}_{h}.pdf"
    if path.exists():
        return None
    path.write_bytes(content)
    return path


def _get(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        if r.status_code == 403:
            ref = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
            r = requests.get(url, headers={**HEADERS, "Referer": ref,
                             "Accept": "application/pdf,*/*"},
                             timeout=30, allow_redirects=True)
        return r
    except requests.exceptions.SSLError:
        return requests.get(url, headers=HEADERS, timeout=30,
                            allow_redirects=True, verify=False)


def fetch(url: str, meta: dict) -> None:
    if not allowed(url):
        print(f"  [robots-blocked] {url}")
        return
    try:
        r = _get(url)
        if r.status_code != 200:
            print(f"  [{r.status_code}] {url}")
            return
        if r.content[:5] != b"%PDF-":
            print(f"  [skip non-pdf] {url}")
            return
        tmp = OUT / f".tmp_{hashlib.sha256(url.encode()).hexdigest()[:8]}.pdf"
        tmp.write_bytes(r.content)
        kind = classify_pdf(tmp)
        tmp.unlink(missing_ok=True)
        if FORMS_ONLY and (kind == "document" or kind.startswith("unreadable")):
            print(f"  [drop {kind}] {url}")
            return
        # fill in drug/specialty for crawled/enumerated URLs that carried no
        # query metadata, by sniffing the URL + filename
        if not meta.get("drug"):
            drug, dmeta = infer_drug(url)
            if drug:
                meta = {**meta, "drug": drug, "drug_class": dmeta["class"],
                        "specialty": dmeta["specialty"], "condition": dmeta["conditions"][0]}
        path = save(url, r.content, kind, meta)
        if path is None:
            print(f"  [skip dup] {url}")
            return
        with INDEX.open("a", newline="") as f:
            csv.writer(f).writerow([
                str(path.relative_to(OUT)), kind, meta.get("specialty", ""),
                meta.get("drug", ""), meta.get("drug_class", ""),
                meta.get("condition", ""), meta.get("payer") or payer_from_url(url), url])
        print(f"  [ok {kind}] {path.relative_to(OUT)}  "
              f"({meta.get('specialty', '?')}/{meta.get('drug_class', '?')})")
    except Exception as e:
        print(f"  [err {type(e).__name__}] {url}")


def crawl_portal(page_url: str) -> list[str]:
    from playwright.sync_api import sync_playwright
    pdfs = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(4000)
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)
            except Exception:
                pass
            for a in page.query_selector_all("a[href]"):
                href = a.get_attribute("href") or ""
                if ".pdf" in href.lower():
                    pdfs.append(urljoin(page_url, href))
        except Exception as e:
            print(f"  [crawl err {type(e).__name__}] {page_url}")
        browser.close()
    pdfs = list(dict.fromkeys(pdfs))
    print(f"  [crawl found {len(pdfs)} pdf links]")
    return pdfs


# ------------------------------------------------------------- REPORT
def coverage_report():
    if not INDEX.exists():
        return
    rows = list(csv.DictReader(INDEX.open()))
    if not rows:
        return
    n = len(rows)
    print(f"\n{'='*60}\nRHEUM/DERM COVERAGE — {n} forms\n{'='*60}")

    by_spec = defaultdict(int)
    for r in rows:
        by_spec[r["specialty"] or "unknown"] += 1
    print("\nBy specialty:")
    for k, v in sorted(by_spec.items(), key=lambda x: -x[1]):
        print(f"  {k:10} {v:4}")

    print("\nBy drug class (variety is the target — every class should show up):")
    by_class = defaultdict(lambda: {"n": 0, "payers": set(), "structs": set()})
    for r in rows:
        c = by_class[r["drug_class"] or "unclassified"]
        c["n"] += 1
        if r["payer"]:
            c["payers"].add(r["payer"])
        c["structs"].add(r["structure"].split("(")[0])
    for cls in DRUG_CLASSES:
        c = by_class.get(cls, {"n": 0, "payers": set(), "structs": set()})
        flag = "  <-- GAP (0 forms)" if c["n"] == 0 else (
               "  <-- thin" if c["n"] < 3 else "")
        print(f"  {cls:22} {c['n']:4}  payers:{len(c['payers']):2}  "
              f"structural:{len(c['structs']):2}{flag}")

    print("\nBy structural type (fill-mechanic variety):")
    by_struct = defaultdict(int)
    for r in rows:
        by_struct[r["structure"].split("(")[0]] += 1
    for k, v in sorted(by_struct.items(), key=lambda x: -x[1]):
        print(f"  {k:16} {v:4}")

    payers = {r["payer"] for r in rows if r["payer"]}
    drugs = {r["drug"] for r in rows if r["drug"]}
    print(f"\nDistinct payers touched: {len(payers)}   distinct drugs touched: {len(drugs)}")
    print(f"Index: {INDEX}")


# ---------------------------------------------------------------- MAIN
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="run dorks via search API")
    ap.add_argument("--payers", action="store_true", help="crawl built-in payer/specialty-pharmacy form pages")
    ap.add_argument("--url-file", help="text file of URLs (manual fallback)")
    ap.add_argument("--portals", nargs="*", default=[], help="extra pages to crawl")
    ap.add_argument("--specialty", choices=["rheum", "derm", "both"], default="both")
    ap.add_argument("--payers-per-drug", type=int, default=5,
                    help="rotating payer subset size per drug (variety, not full cross product)")
    ap.add_argument("--no-condition-dorks", action="store_true")
    ap.add_argument("--no-drug-dorks", action="store_true")
    ap.add_argument("--no-payer-dorks", action="store_true")
    ap.add_argument("--fill-gaps", action="store_true",
                    help="prioritize (specialty, drug_class) cells with the fewest forms so far")
    ap.add_argument("--per-query", type=int, default=20)
    ap.add_argument("--max-queries", type=int, default=80,
                    help="cap total dork queries to protect API quota")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--ignore-robots", action="store_true")
    ap.add_argument("--forms-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the query plan, download nothing")
    args = ap.parse_args()

    global IGNORE_ROBOTS, FORMS_ONLY
    IGNORE_ROBOTS = args.ignore_robots
    FORMS_ONLY = args.forms_only

    if not INDEX.exists():
        INDEX.write_text(",".join(INDEX_COLS) + "\n")

    all_targets: list[tuple[str, dict]] = []  # (url, meta)

    if args.discover:
        if _provider() is None and not args.dry_run:
            print("!! No search API key set (BRAVE_API_KEY / SERPAPI_KEY / "
                  "GOOGLE_CSE_KEY+CX). Skipping --discover.")
        else:
            records = build_dork_records(
                specialty=args.specialty, payers_per_drug=args.payers_per_drug,
                no_condition=args.no_condition_dorks, no_drug=args.no_drug_dorks,
                no_payer=args.no_payer_dorks)
            records = interleave_variety(records)
            if args.fill_gaps:
                records = prioritize_gaps(records, existing_coverage(INDEX))
            if len(records) > args.max_queries:
                print(f"  ({len(records)} queries built; capping at {args.max_queries} "
                      f"to protect API quota — variety-first order means the cap "
                      f"still spans every drug class/specialty)")
                records = records[: args.max_queries]
            print(f"== discover: {len(records)} dork queries ({args.specialty}) ==")
            for i, rec in enumerate(records, 1):
                tag = f"{rec['specialty']}/{rec['drug_class'] or rec['condition']}"
                if args.dry_run:
                    print(f"  [{i:>3}] ({tag}) {rec['query']}")
                    continue
                hits = search(rec["query"], args.per_query)
                print(f"  [{i:>3}/{len(records)}] ({tag}) [{len(hits):>2}] {rec['query']}")
                all_targets += [(u, rec) for u in hits]
                time.sleep(args.delay)

    if args.url_file:
        urls = [l.strip() for l in Path(args.url_file).read_text().splitlines() if l.strip()]
        all_targets += [(u, {}) for u in urls]

    portals = list(args.portals) + (PAYER_PORTALS if args.payers else [])
    for portal in portals:
        if args.dry_run:
            print(f"  [would crawl] {portal}")
            continue
        print(f"== crawl {portal} ==")
        for u in crawl_portal(portal):
            all_targets.append((u, {}))

    # de-dup URLs, keep first metadata seen
    seen, deduped = set(), []
    for u, meta in all_targets:
        if u not in seen:
            seen.add(u)
            deduped.append((u, meta))

    if args.dry_run:
        print(f"\n[dry-run] would download {len(deduped)} candidate URLs; no files written.")
        return

    if not deduped:
        if not (args.discover or args.url_file or portals):
            ap.print_help()
        return

    print(f"\n== download {len(deduped)} candidate URLs ==")
    for url, meta in deduped:
        fetch(url, meta)
        time.sleep(args.delay)

    coverage_report()


if __name__ == "__main__":
    main()
