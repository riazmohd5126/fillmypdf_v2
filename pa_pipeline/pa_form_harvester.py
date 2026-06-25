#!/usr/bin/env python3
"""
pa_form_harvester.py  (v2 — fully automated)

End-to-end pipeline to collect blank Prior Authorization PDFs for autofill
testing. Three stages, all automatic:

  1. DISCOVER  - run Google dorks through a search API (Brave / SerpAPI / CSE)
  2. ENUMERATE - generate known per-state URL patterns (Caremark, Aetna)
  3. CRAWL     - render payer form-library pages and scrape PDF links
Then every PDF is downloaded, de-duplicated by hash, and CLASSIFIED by
structure (acroform / xfa / flat) so you get a balanced test corpus.

------------------------------------------------------------------ SETUP
  pip install requests "pypdf[crypto]" playwright
  playwright install chromium          # only needed for --payers / crawling

  Set ONE search-API key as an env var to enable --discover:
    export BRAVE_API_KEY=...           # api.search.brave.com (free tier)
    # or
    export SERPAPI_KEY=...             # serpapi.com
    # or
    export GOOGLE_CSE_KEY=...  GOOGLE_CSE_CX=...   # Google Custom Search

------------------------------------------------------------------ RUN
  # The whole thing, one command:
  python pa_form_harvester.py --all

  # Or pick stages:
  python pa_form_harvester.py --discover
  python pa_form_harvester.py --payers --aetna-states --caremark-states
  python pa_form_harvester.py --url-file my_urls.txt     # manual fallback

  # Automate on a schedule (weekly):
  #   crontab -e
  #   0 3 * * 1  cd /path && /usr/bin/python3 pa_form_harvester.py --all >> harvest.log 2>&1
"""

import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

try:
    requests.packages.urllib3.disable_warnings()  # quiet SSL-relaxed retries
except Exception:
    pass

OUT = Path("pa_forms")
OUT.mkdir(exist_ok=True)
INDEX = OUT / "_index.csv"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36")}

# ---------------------------------------------------------------- DORKS
# A few hand-written base queries, kept for the broad net...
DORKS_BASE = [
    'filetype:pdf "prior authorization request form" "fax"',
    'filetype:pdf "standardized prior authorization" form',
    'filetype:pdf "uniform prior authorization" form',
    'filetype:pdf "medication prior authorization" (site:.gov OR site:.us)',
    'filetype:pdf "prior authorization" medicaid form',
]

# ...plus a combinatorial builder: payers x form-type/drug terms.
# This is what closes the coverage gap — each combo surfaces a distinct
# set of forms (each payer has a separate form per high-cost drug).
PAYER_DOMAINS = [
    "uhcprovider.com", "aetna.com", "cigna.com", "humana.com", "anthem.com",
    "highmark.com", "floridablue.com", "bcbs.com", "bcbsil.com", "bcbstx.com",
    "carefirst.com", "amerihealth.com", "ibx.com", "wellsense.org",
    "fideliscare.org", "superiorhealthplan.com", "pahealthwellness.com",
    "providers.anthem.com", "molinahealthcare.com", "centene.com",
    "healthnet.com", "kaiserpermanente.org", "cvs.com", "express-scripts.com",
    "optumrx.com", "primetherapeutics.com",
]
FORM_TERMS = [
    '"prior authorization request form"',
    '"prior authorization" fax form',
    '"medication prior authorization"',
    '"specialty drug" prior authorization form',
    '"injectable" prior authorization form',
    'prior authorization form',
]
# High-cost specialty drugs — each tends to have its own per-payer PA form.
DRUG_TERMS = [
    "Humira", "Skyrizi", "Stelara", "Dupixent", "Enbrel", "Cosentyx",
    "Rinvoq", "Taltz", "Tremfya", "Otezla", "Ozempic", "Wegovy", "Mounjaro",
    "Zepbound", "Botox", "Xolair", "Prolia", "Spravato", "Repatha",
    "Eliquis", "Trikafta", "Vyvanse", "Adderall",
]

def build_dorks(use_drugs=True, use_payers=True):
    qs = list(DORKS_BASE)
    if use_payers:
        for d in PAYER_DOMAINS:
            for t in FORM_TERMS:
                qs.append(f'site:{d} filetype:pdf {t}')
    if use_drugs:
        for drug in DRUG_TERMS:
            qs.append(f'filetype:pdf "prior authorization" {drug} form')
    return list(dict.fromkeys(qs))

# ------------------------------------------------------- ENUMERABLE PATHS
# NOTE: Caremark state-form filenames are NOT uniform — some states use
# _State_PA_Request_Form, others _Prior_Authorization_Form. We try both;
# 404s fail silently. For full coverage, prefer crawling the index pages.
CAREMARK_BASE = ("https://www.caremark.com/content/dam/enterprise/caremark/"
                 "pdfs/pa-state-requirements/{state}_{suffix}.pdf")
CAREMARK_SUFFIXES = ["State_PA_Request_Form", "Prior_Authorization_Form"]
AETNA_TPL = ("https://www.aetna.com/content/dam/aetna/pdfs/aetnacom/"
             "healthcare-professionals/documents-forms/"
             "{st}-medical-exception-prior-authorization-form.pdf")
US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New_Hampshire", "New_Jersey",
    "New_Mexico", "New_York", "North_Carolina", "North_Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode_Island", "South_Carolina",
    "South_Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West_Virginia", "Wisconsin", "Wyoming",
]
STATE_CODES = [s[:2].lower() if "_" not in s else s.split("_")[0][:1].lower() + s.split("_")[1][:1].lower() for s in US_STATES]
# (simpler explicit list to avoid surprises:)
STATE_CODES = [
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy",
]

SEEDS = [
    "https://www.tdi.texas.gov/forms/lhlifehealth/nofr001.pdf",
    "https://www.mass.gov/doc/massachusetts-standard-form-for-medication-prior-authorization-request/download",
    "https://www.michigan.gov/-/media/Project/Websites/difs/Form/Insurance/Prior_Auth/FIS_2288.pdf",
]

PAYER_PORTALS = [
    "https://info.caremark.com/dig/pa-forms",  # Caremark drug-specific PA library (crawl, don't guess)
    "https://www.uhcprovider.com/en/resource-library/provider-forms.html",
    "https://www.aetna.com/health-care-professionals/precertification/precertification-lists.html",
    "https://www.cigna.com/health-care-providers/coverage-and-claims/precertification",
    "https://provider.humana.com/coverage-claims/prior-authorizations",
    # Add regional BCBS form pages for max layout diversity, e.g.:
    # "https://www.anthem.com/provider/forms/",
    # "https://www.floridablue.com/providers/tools-resources/forms",
]

# ---------------------------------------------------- ROBOTS (politeness)
_robots_cache: dict[str, RobotFileParser] = {}
IGNORE_ROBOTS = False  # set True via --ignore-robots
FORMS_ONLY = False     # set True via --forms-only

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
            rp = None  # no robots reachable -> allow
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
    """Return result URLs for a dork query via whichever API key is set."""
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

def discover(per_query: int = 20, delay: float = 1.0,
             max_queries: int = 60, use_drugs: bool = True,
             use_payers: bool = True) -> list[str]:
    if _provider() is None:
        print("!! No search API key set (BRAVE_API_KEY / SERPAPI_KEY / "
              "GOOGLE_CSE_KEY+CX). Skipping --discover.")
        return []
    queries = build_dorks(use_drugs=use_drugs, use_payers=use_payers)
    if len(queries) > max_queries:
        print(f"  ({len(queries)} queries built; capping at {max_queries} "
              f"to protect API quota — raise with --max-queries)")
        queries = queries[:max_queries]
    found: list[str] = []
    for q in queries:
        hits = search(q, per_query)
        print(f"  [{len(hits):>2}] {q}")
        found += hits
        time.sleep(delay)
    return list(dict.fromkeys(found))

# ------------------------------------------------------------- CLASSIFY
# Request forms have labels/fields; criteria docs are prose. We separate:
#   acroform(N) / xfa  -> fillable
#   form-flat          -> flat but form-shaped (OCR/overlay autofill path)
#   document           -> prose (clinical criteria etc.) — NOT a form
FORM_TOKENS = ["patient name", "date of birth", "dob", "member id", "fax",
               "prescriber", "npi", "signature", "diagnosis", "icd",
               "authorization request", "request form"]

def classify_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")          # empty user password (very common)
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
        score += text.count("\u2610")
        return "form-flat" if score >= 3 else "document"
    except Exception as e:
        return f"unreadable({type(e).__name__})"

# ------------------------------------------------------------- DOWNLOAD
def category(kind: str) -> str:
    """Top-level subfolder by structure type."""
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
    """Registrable-domain label as payer: assets.humana.com -> humana,
    static.cigna.com -> cigna, www.uhcprovider.com -> uhcprovider."""
    labels = [l for l in urlparse(url).netloc.replace("www.", "").split(".") if l]
    if len(labels) >= 2:
        return labels[-2]            # the name before the TLD
    return labels[0] if labels else "unknown"

def save(url: str, content: bytes, kind: str) -> Path | None:
    """Write into pa_forms/<category>/<payer>/ ; dedupe by content hash."""
    if not content[:5] == b"%PDF-":
        return None
    h = hashlib.sha256(content).hexdigest()[:12]
    host = payer_from_url(url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", Path(urlparse(url).path).stem)[:50]
    folder = OUT / category(kind) / host
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{host}_{slug}_{h}.pdf"
    if path.exists():
        return None
    path.write_bytes(content)
    return path

def _get(url: str):
    """GET with one polite retry for 403 (add referer) and SSL (relax verify)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        if r.status_code == 403:
            ref = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
            r = requests.get(url, headers={**HEADERS, "Referer": ref,
                             "Accept": "application/pdf,*/*"},
                             timeout=30, allow_redirects=True)
        return r
    except requests.exceptions.SSLError:
        # cert problems on some plan sites; retry once without verification
        return requests.get(url, headers=HEADERS, timeout=30,
                            allow_redirects=True, verify=False)

def fetch(url: str) -> None:
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
        # classify from a temp write, then place into the right subfolder
        tmp = OUT / f".tmp_{hashlib.sha256(url.encode()).hexdigest()[:8]}.pdf"
        tmp.write_bytes(r.content)
        kind = classify_pdf(tmp)
        tmp.unlink(missing_ok=True)
        if FORMS_ONLY and (kind == "document" or kind.startswith("unreadable")):
            print(f"  [drop {kind}] {url}")
            return
        path = save(url, r.content, kind)
        if path is None:
            print(f"  [skip dup] {url}")
            return
        with INDEX.open("a") as f:
            f.write(f"{path.relative_to(OUT)},{kind},{url}\n")
        print(f"  [ok {kind}] {path.relative_to(OUT)}")
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
            # networkidle never settles on chatty SPAs; domcontentloaded is
            # reliable, then we pause + scroll to trigger lazy-loaded links.
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

# ---------------------------------------------------------------- MAIN
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="discover + enumerate + crawl, everything")
    ap.add_argument("--discover", action="store_true", help="run dorks via search API")
    ap.add_argument("--caremark-states", action="store_true")
    ap.add_argument("--aetna-states", action="store_true")
    ap.add_argument("--payers", action="store_true", help="crawl built-in payer form pages")
    ap.add_argument("--seeds", action="store_true")
    ap.add_argument("--url-file", help="text file of URLs (manual fallback)")
    ap.add_argument("--portals", nargs="*", default=[], help="extra pages to crawl")
    ap.add_argument("--per-query", type=int, default=20, help="results per dork (Brave caps at 20)")
    ap.add_argument("--max-queries", type=int, default=60,
                    help="cap total dork queries to protect API quota")
    ap.add_argument("--no-drugs", action="store_true", help="skip per-drug queries")
    ap.add_argument("--no-payer-queries", action="store_true", help="skip per-payer queries")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--ignore-robots", action="store_true",
                    help="skip robots.txt checks (you accept responsibility for ToS/rate)")
    ap.add_argument("--forms-only", action="store_true",
                    help="discard prose 'document' (criteria) and unreadable PDFs; keep only forms")
    args = ap.parse_args()

    global IGNORE_ROBOTS, FORMS_ONLY
    IGNORE_ROBOTS = args.ignore_robots
    FORMS_ONLY = args.forms_only

    if args.all:
        args.discover = args.caremark_states = args.aetna_states = args.payers = args.seeds = True

    if not INDEX.exists():
        INDEX.write_text("file,structure,source_url\n")

    targets: list[str] = []
    if args.seeds:
        targets += SEEDS
    if args.caremark_states:
        targets += [CAREMARK_BASE.format(state=s, suffix=suf)
                    for s in US_STATES for suf in CAREMARK_SUFFIXES]
    if args.aetna_states:
        targets += [AETNA_TPL.format(st=c) for c in STATE_CODES]
    if args.discover:
        print("== discover ==")
        targets += discover(args.per_query, args.delay, args.max_queries,
                             use_drugs=not args.no_drugs,
                             use_payers=not args.no_payer_queries)
    if args.url_file:
        targets += [l.strip() for l in Path(args.url_file).read_text().splitlines() if l.strip()]
    portals = list(args.portals) + (PAYER_PORTALS if args.payers else [])
    for portal in portals:
        print(f"== crawl {portal} ==")
        targets += crawl_portal(portal)

    targets = list(dict.fromkeys(targets))
    if not targets:
        ap.print_help()
        sys.exit(1)

    print(f"\n== download {len(targets)} candidate URLs ==")
    for url in targets:
        fetch(url)
        time.sleep(args.delay)

    # ---- summary: structure mix per payer ----
    rows = [l.split(",") for l in INDEX.read_text().splitlines()[1:]]
    by_payer: dict[str, dict[str, int]] = {}
    for relpath, kind, _ in (r for r in rows if len(r) == 3):
        # relpath is "<category>/<payer>/<file>.pdf"
        parts = relpath.split("/")
        payer = parts[1] if len(parts) >= 3 else parts[-1].split("_")[0]
        bucket = "acroform" if kind.startswith("acroform") else kind.split("(")[0]
        by_payer.setdefault(payer, {}).setdefault(bucket, 0)
        by_payer[payer][bucket] += 1
    print(f"\n== corpus: {len(rows)} forms ==")
    for payer, kinds in sorted(by_payer.items()):
        print(f"  {payer:22} " + "  ".join(f"{k}:{v}" for k, v in sorted(kinds.items())))
    print(f"\nFiles organized under: {OUT}/<acroform|form-flat|xfa>/<payer>/")
    print(f"Index: {INDEX}")

if __name__ == "__main__":
    main()
