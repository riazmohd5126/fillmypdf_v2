#!/usr/bin/env python3
"""
Fillable Prior Authorization PDF Downloader — Reliable Edition
Uses five free sources to find and download fillable prior auth PDFs.

Sources (all free, no credit card):
  1. Wayback Machine CDX API  — glob patterns like *prior*auth*.pdf across
     all of archive.org. Downloads served by archive.org — never 404.
  2. Common Crawl CDX API     — domain + keyword URL filter across CC index.
  3. HTML page crawler        — crawls real insurer provider portal pages.
  4. SearXNG                  — open-source meta-search that proxies Google
     AND Bing dorks. Use any public instance, no account needed.
  5. Google Custom Search API — 100 free queries/day, needs Google account
     only (no credit card). Pass --google-key + --google-cx.

All PDFs validated: must be ≥20 KB and have AcroForm fields (fillable).

Requirements:
    pip install requests beautifulsoup4 lxml tqdm pypdf

Usage:
    # All sources (SearXNG + archives + crawl):
    python get_fillable_prior_auth.py

    # With Google CSE (best targeted results):
    python get_fillable_prior_auth.py --google-key KEY --google-cx CX

    # Skip slow sources, just run SearXNG:
    python get_fillable_prior_auth.py --sources searx

    # Custom output folder + verbose:
    python get_fillable_prior_auth.py --output ~/Desktop/pa_forms --verbose

How to get Google CSE key (free, no credit card):
    1. Go to console.cloud.google.com → New project → Enable "Custom Search API"
    2. Create API key under "Credentials"
    3. Go to programmablesearchengine.google.com → New engine → Search the web
    4. Copy the CX (Search engine ID)
    → 100 free queries/day, no billing required
"""

import io
import json
import os
import re
import time
import hashlib
import logging
import argparse
import random
from pathlib import Path
from urllib.parse import urlparse, unquote, urljoin, quote

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
MIN_PDF_BYTES = 20_000      # 20 KB — stub/redirect PDFs are always smaller
MIN_FIELD_COUNT = 1         # must have at least 1 AcroForm field

# ── Wayback Machine CDX search patterns ──────────────────────────────────────
# These are URL glob patterns sent to the CDX API.
# The API indexes billions of pages; patterns with * match any segment.
WB_PATTERNS: list[tuple[str, str]] = [
    ("*prior*authorization*form*.pdf",          "generic"),
    ("*prior*auth*request*.pdf",                "generic"),
    ("*prior*authorization*request*.pdf",       "generic"),
    ("*pa*request*form*.pdf",                   "generic"),
    ("*prior*auth*fillable*.pdf",               "generic"),
    ("*prior*authorization*pharmacy*.pdf",      "medication"),
    ("*prior*auth*drug*.pdf",                   "medication"),
    ("*prior*auth*medication*.pdf",             "medication"),
    ("*prior*auth*specialty*.pdf",              "specialty"),
    ("*prior*auth*glp*.pdf",                    "specialty/glp1"),
    ("*prior*auth*biolog*.pdf",                 "specialty/biologics"),
    ("*prior*auth*oncol*.pdf",                  "specialty/oncology"),
    ("*step*therapy*prior*auth*.pdf",           "step_therapy"),
    ("*uhc*prior*auth*.pdf",                    "insurers/uhc"),
    ("*uhcprovider*prior*.pdf",                 "insurers/uhc"),
    ("*cigna*prior*auth*.pdf",                  "insurers/cigna"),
    ("*aetna*prior*auth*.pdf",                  "insurers/aetna"),
    ("*anthem*prior*auth*.pdf",                 "insurers/anthem"),
    ("*humana*prior*auth*.pdf",                 "insurers/humana"),
    ("*bcbs*prior*auth*.pdf",                   "insurers/bcbs"),
    ("*caremark*prior*auth*.pdf",               "insurers/caremark"),
    ("*optum*prior*auth*.pdf",                  "insurers/optum"),
    ("*medicaid*prior*auth*.pdf",               "medicaid"),
    ("*medicaid*authorization*form*.pdf",       "medicaid"),
]

# ── Common Crawl domain + keyword patterns ────────────────────────────────────
# (url_pattern, keyword_must_appear_in_url, folder)
CC_PATTERNS: list[tuple[str, str, str]] = [
    ("*.uhcprovider.com/*.pdf",        "prior",         "insurers/uhc"),
    ("*.uhc.com/*.pdf",                "auth",          "insurers/uhc"),
    ("*.cigna.com/*.pdf",              "prior",         "insurers/cigna"),
    ("*.aetna.com/*.pdf",              "auth",          "insurers/aetna"),
    ("*.anthem.com/*.pdf",             "prior",         "insurers/anthem"),
    ("*.humana.com/*.pdf",             "prior",         "insurers/humana"),
    ("*.bcbsil.com/*.pdf",             "prior",         "insurers/bcbs"),
    ("*.bcbstx.com/*.pdf",             "prior",         "insurers/bcbs"),
    ("*.bcbsma.com/*.pdf",             "prior",         "insurers/bcbs"),
    ("*.bcbsnc.com/*.pdf",             "auth",          "insurers/bcbs"),
    ("*.caremark.com/*.pdf",           "prior",         "insurers/caremark"),
    ("*.optumrx.com/*.pdf",            "prior",         "insurers/optum"),
    ("*.optum.com/*.pdf",              "auth",          "insurers/optum"),
    ("*.humana.com/*.pdf",             "auth",          "insurers/humana"),
    ("*.molina*.com/*.pdf",            "prior",         "insurers/molina"),
    ("*.wellcare.com/*.pdf",           "auth",          "insurers/wellcare"),
    ("*.centene.com/*.pdf",            "prior",         "insurers/centene"),
    ("*.cms.gov/*.pdf",                "prior",         "cms"),
    ("*.medicaid.gov/*.pdf",           "auth",          "medicaid"),
    ("*.hfs.illinois.gov/*.pdf",       "prior",         "medicaid/il"),
    ("*.health.ny.gov/*.pdf",          "auth",          "medicaid/ny"),
    ("*.medi-cal.ca.gov/*.pdf",        "prior",         "medicaid/ca"),
]

# ── Real HTML landing pages to crawl for embedded PDF links ──────────────────
CRAWL_PAGES: list[tuple[str, str]] = [
    ("https://www.uhcprovider.com/en/prior-auth-advance-notification.html",          "insurers/uhc"),
    ("https://www.uhcprovider.com/en/resource-library/prior-authorization.html",     "insurers/uhc"),
    ("https://www.cigna.com/healthcare-providers/coverage-and-claims/prior-authorization", "insurers/cigna"),
    ("https://www.anthem.com/provider/prior-authorization/",                         "insurers/anthem"),
    ("https://www.humana.com/provider/medical-resources/prior-authorization",        "insurers/humana"),
    ("https://www.bcbsil.com/provider/clinical/prior_authorization.html",            "insurers/bcbs"),
    ("https://www.bcbstx.com/provider/clinical/prior_authorization.html",            "insurers/bcbs"),
    ("https://www.caremark.com/wps/portal/prescriber",                               "insurers/caremark"),
    ("https://www.optumrx.com/oe/sca/prior-authorization",                          "insurers/optum"),
    ("https://www.cms.gov/medicare/prior-authorization-and-pre-claim-review-initiatives", "cms"),
    ("https://www.molinahealthcare.com/providers/wa/medicaid/auth/priorauth.aspx",   "insurers/molina"),
    ("https://provider.carefirst.com/carefirst-resources/provider/px-clinical-criteria.page", "insurers/bcbs"),
]

# ── Google dork queries (used by SearXNG and Google CSE) ─────────────────────
GOOGLE_DORKS: list[tuple[str, str]] = [
    # (query, output_folder)
    ('filetype:pdf "prior authorization form" fillable',                        "generic"),
    ('filetype:pdf "prior authorization request" form fillable',                "generic"),
    ('filetype:pdf "prior authorization" "patient name" "date of birth" "prescriber"', "generic"),
    ('filetype:pdf "prior authorization" "fax" "date of birth" "prescriber"',  "generic"),
    ('filetype:pdf "prior authorization" "procedure code" OR "CPT code"',       "procedure"),
    ('filetype:pdf "prior authorization" "drug name" OR "NDC"',                 "medication"),
    ('filetype:pdf "prior authorization" "GLP-1" OR "semaglutide" OR "Ozempic"', "specialty/glp1"),
    ('filetype:pdf "prior authorization" "biologic" fillable',                  "specialty/biologics"),
    ('filetype:pdf "prior authorization" "oncology" request',                   "specialty/oncology"),
    ('filetype:pdf "prior authorization" "rheumatology" fillable',              "specialty/rheumatology"),
    ('filetype:pdf "prior authorization" "step therapy" form',                  "step_therapy"),
    ('filetype:pdf "prior authorization" "Adobe LiveCycle"',                    "livecycle"),
    ('filetype:pdf "prior authorization" site:uhc.com',                         "insurers/uhc"),
    ('filetype:pdf "prior authorization" site:cigna.com',                       "insurers/cigna"),
    ('filetype:pdf "prior authorization" site:aetna.com',                       "insurers/aetna"),
    ('filetype:pdf "prior authorization" site:anthem.com',                      "insurers/anthem"),
    ('filetype:pdf "prior authorization" site:humana.com',                      "insurers/humana"),
    ('filetype:pdf "prior authorization" site:caremark.com',                    "insurers/caremark"),
    ('filetype:pdf "prior authorization" site:optumrx.com',                     "insurers/optum"),
    ('filetype:pdf "prior authorization" site:cms.gov',                         "cms"),
    ('filetype:pdf "prior authorization" site:medicaid.gov',                    "medicaid"),
]

# ── Public SearXNG instances (try in order, skip if down) ────────────────────
SEARX_INSTANCES: list[str] = [
    "https://searx.be",
    "https://searxng.site",
    "https://search.ononoki.org",
    "https://paulgo.io",
    "https://searx.tiekoetter.com",
    "https://searx.colbster937.dev",
]

# ── HTTP session ──────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
})

_seen_hashes: set[str] = set()
_seen_urls:   set[str] = set()


# ─────────────────────────────────────────────────────────────────────────────
# Source 1 — Wayback Machine CDX API
# ─────────────────────────────────────────────────────────────────────────────

def wayback_search(pattern: str, limit: int = 200) -> list[tuple[str, str]]:
    """
    Search the Wayback Machine CDX index for URLs matching a glob pattern.
    Returns list of (original_url, wayback_url) tuples.
    The wayback_url is always fetchable even if the original is gone.

    API docs: https://github.com/internetarchive/wayback/blob/master/wayback-cdx-server/README.md
    """
    params = {
        "url":        pattern,
        "output":     "json",
        "fl":         "timestamp,original,mimetype,statuscode",
        "filter":     ["mimetype:application/pdf", "statuscode:200"],
        "collapse":   "urlkey",        # one result per unique URL
        "limit":      limit,
    }
    try:
        resp = SESSION.get(
            "https://web.archive.org/cdx/search/cdx",
            params=params,
            timeout=60,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        log.warning("Wayback CDX failed for '%s': %s", pattern, exc)
        return []

    if not rows or len(rows) <= 1:
        return []

    results = []
    for row in rows[1:]:            # row[0] is the header
        if len(row) < 4:
            continue
        timestamp, original, mime, status = row[0], row[1], row[2], row[3]
        if status != "200":
            continue
        wayback = f"https://web.archive.org/web/{timestamp}if_/{original}"
        results.append((original, wayback))

    log.info("  Wayback '%s'  → %d hits", pattern, len(results))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Source 2 — Common Crawl CDX API
# ─────────────────────────────────────────────────────────────────────────────

_CC_CRAWL_ID: str = ""

def get_cc_crawl_id() -> str:
    global _CC_CRAWL_ID
    if _CC_CRAWL_ID:
        return _CC_CRAWL_ID
    try:
        resp = SESSION.get("https://index.commoncrawl.org/collinfo.json", timeout=20)
        resp.raise_for_status()
        _CC_CRAWL_ID = resp.json()[0]["id"]
        log.info("Common Crawl snapshot: %s", _CC_CRAWL_ID)
    except Exception as exc:
        log.warning("Could not fetch CC crawl list: %s", exc)
        _CC_CRAWL_ID = "CC-MAIN-2024-10"
    return _CC_CRAWL_ID


def cc_search(url_pattern: str, keyword: str, limit: int = 200) -> list[str]:
    """
    Query Common Crawl CDX for a domain pattern, then filter by keyword in URL.
    Returns original URLs (must download from live site).
    """
    crawl_id = get_cc_crawl_id()
    endpoint = f"https://index.commoncrawl.org/{crawl_id}-index"
    params = {
        "url":      url_pattern,
        "output":   "json",
        "filter":   "mime:application/pdf",
        "fl":       "url,status",
        "limit":    limit,
        "collapse": "urlkey",
    }
    try:
        resp = SESSION.get(endpoint, params=params, timeout=60)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("CC CDX failed for '%s': %s", url_pattern, exc)
        return []

    urls = []
    for line in resp.text.strip().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("status") != "200":
            continue
        url = rec.get("url", "")
        if url and keyword.lower() in url.lower():
            urls.append(url)

    log.info("  CC '%s' +keyword='%s'  → %d URLs", url_pattern, keyword, len(urls))
    return urls


# ─────────────────────────────────────────────────────────────────────────────
# Source 3 — HTML page crawler
# ─────────────────────────────────────────────────────────────────────────────

PRIOR_AUTH_KEYWORDS = [
    "prior", "auth", "pa-form", "pa_form", "preauth",
    "precert", "step-therapy", "formulary", "utilization",
]

def crawl_page(page_url: str) -> list[str]:
    """Load an HTML page, return all PDF hrefs that look like prior-auth forms."""
    try:
        resp = SESSION.get(
            page_url, timeout=20, allow_redirects=True,
            headers={"Accept": "text/html,*/*"},
        )
        resp.raise_for_status()
    except Exception as exc:
        log.debug("Crawl failed (%s): %s", type(exc).__name__, page_url)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    found = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith("#"):
            continue
        full = urljoin(page_url, href)
        if not full.lower().endswith(".pdf"):
            continue
        lower = full.lower()
        if any(kw in lower for kw in PRIOR_AUTH_KEYWORDS):
            found.append(full)

    log.info("  Crawled %-65s → %d PDF links", page_url, len(found))
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Source 4 — SearXNG  (proxies Google + Bing dorks, no account needed)
# ─────────────────────────────────────────────────────────────────────────────

_searx_instance: str = ""

def get_searx_instance() -> str:
    """Return the first reachable SearXNG instance."""
    global _searx_instance
    if _searx_instance:
        return _searx_instance
    for base in SEARX_INSTANCES:
        try:
            r = SESSION.get(
                f"{base}/search",
                params={"q": "test", "format": "json"},
                timeout=8,
                headers={"Accept": "application/json"},
            )
            if r.status_code == 200:
                _searx_instance = base
                log.info("SearXNG: using %s", base)
                return base
        except Exception:
            continue
    log.warning("SearXNG: no reachable instance found")
    return ""


def searx_search(query: str, max_results: int = 50) -> list[str]:
    """
    Search via a public SearXNG instance.
    Sends real Google/Bing dorks — filetype:pdf is honored.
    Returns PDF URLs extracted from results.
    """
    base = get_searx_instance()
    if not base:
        return []

    urls: list[str] = []
    for page in range(1, (max_results // 10) + 2):
        params = {
            "q":       query,
            "format":  "json",
            "engines": "google,bing,duckduckgo",
            "pageno":  page,
        }
        try:
            resp = SESSION.get(
                f"{base}/search",
                params=params,
                timeout=20,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.debug("SearXNG error page=%d: %s", page, exc)
            break

        results = data.get("results", [])
        if not results:
            break

        for r in results:
            url = r.get("url", "")
            if url and ".pdf" in url.lower():
                urls.append(url)

        if len(results) < 10:
            break
        if len(urls) >= max_results:
            break
        time.sleep(random.uniform(1.5, 3.0))

    log.info("  SearXNG '%s'  → %d PDF URLs", query[:65], len(urls))
    return list(dict.fromkeys(urls))


# ─────────────────────────────────────────────────────────────────────────────
# Source 5 — Google Custom Search API  (optional, 100 free queries/day)
# ─────────────────────────────────────────────────────────────────────────────

def google_cse_search(query: str, api_key: str, cx: str, max_results: int = 100) -> list[str]:
    """
    Google Custom Search JSON API.
    Supports filetype:pdf natively via fileType param.
    Free tier: 100 queries/day, Google account only (no credit card).

    Setup (5 min, free):
      1. console.cloud.google.com → new project → enable "Custom Search API"
      2. Credentials → Create API key
      3. programmablesearchengine.google.com → New engine → "Search the whole web"
      4. Copy CX (Search engine ID)
    """
    endpoint = "https://www.googleapis.com/customsearch/v1"
    urls: list[str] = []
    start = 1

    clean_q = re.sub(r"filetype:\w+", "", query).strip()
    ft_match = re.search(r"filetype:(\w+)", query, re.I)
    filetype = ft_match.group(1) if ft_match else None

    while start <= min(max_results, 91):
        params: dict = {"key": api_key, "cx": cx, "q": clean_q, "num": 10, "start": start}
        if filetype:
            params["fileType"] = filetype
        try:
            resp = SESSION.get(endpoint, params=params, timeout=15)
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if not items:
                break
            urls.extend(i["link"] for i in items if i.get("link"))
            start += len(items)
            if len(items) < 10:
                break
        except Exception as exc:
            log.warning("  Google CSE error: %s", exc)
            break
        time.sleep(1.0)

    log.info("  Google CSE '%s'  → %d results", query[:65], len(urls))
    return urls


# ─────────────────────────────────────────────────────────────────────────────
# PDF download + validation
# ─────────────────────────────────────────────────────────────────────────────

def fetch_bytes(url: str, timeout: int = 30) -> bytes | None:
    """Fetch a URL and return raw bytes, or None on failure."""
    try:
        resp = SESSION.get(
            url, timeout=timeout, stream=True, allow_redirects=True,
            headers={"Accept": "application/pdf,*/*"},
        )
        resp.raise_for_status()
        data = b"".join(resp.iter_content(8192))
        return data
    except Exception as exc:
        log.debug("Fetch failed (%s): %s", type(exc).__name__, url)
        return None


def count_pdf_fields(data: bytes) -> int:
    """Return number of AcroForm fields in PDF, -1 if pypdf unavailable."""
    if not HAS_PYPDF:
        return -1
    try:
        reader = PdfReader(io.BytesIO(data))
        fields = reader.get_fields() or {}
        return len(fields)
    except Exception:
        return 0


def validate_and_save(
    data: bytes,
    url: str,
    dest_dir: Path,
) -> tuple[bool, str]:
    """Run all checks and save if valid. Returns (saved, reason)."""

    # 1. PDF magic bytes
    if not data.startswith(b"%PDF"):
        return False, "not a PDF"

    # 2. Minimum size
    if len(data) < MIN_PDF_BYTES:
        return False, f"too small ({len(data)//1024} KB)"

    # 3. Duplicate content
    h = hashlib.md5(data).hexdigest()
    if h in _seen_hashes:
        return False, "duplicate content"
    _seen_hashes.add(h)

    # 4. Fillable check
    n_fields = count_pdf_fields(data)
    if HAS_PYPDF and n_fields == 0:
        return False, "not fillable (0 AcroForm fields)"

    # Save
    dest_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    base = os.path.basename(parsed.path) or "form"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    domain = parsed.netloc.replace("www.", "").split(".")[0]
    fname = re.sub(r'[\\/*?:"<>|\']+', "_", f"{domain}_{base}")[:120]
    dest = dest_dir / fname
    if dest.exists():
        dest = dest_dir / f"{dest.stem}_{h[:6]}.pdf"

    dest.write_bytes(data)
    fields_str = f"{n_fields} fields" if n_fields >= 0 else "fields unknown"
    log.info("  SAVED  %-60s  %d KB  [%s]", dest.name, len(data)//1024, fields_str)
    return True, f"OK ({fields_str})"


def process_url(url: str, dest_dir: Path, delay: float) -> tuple[bool, str]:
    if url in _seen_urls:
        return False, "duplicate URL"
    _seen_urls.add(url)
    data = fetch_bytes(url)
    time.sleep(delay)
    if data is None:
        return False, "fetch failed"
    return validate_and_save(data, url, dest_dir)


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def run(
    base_dir: Path,
    sources: set[str],
    wb_limit: int,
    cc_limit: int,
    download_delay: float,
    google_key: str = "",
    google_cx: str = "",
    searx_results: int = 30,
) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    tried = 0
    reasons: dict[str, int] = {}

    def attempt(url: str, folder: str) -> None:
        nonlocal saved, tried
        tried += 1
        ok, reason = process_url(url, base_dir / folder, download_delay)
        if ok:
            saved += 1
        else:
            reasons[reason] = reasons.get(reason, 0) + 1
            log.debug("  SKIP  (%s)  %s", reason, url[:90])

    # ── Source 1: Wayback Machine ─────────────────────────────────────────────
    if "wayback" in sources:
        print(f"\n{'='*62}")
        print("SOURCE 1 — Wayback Machine CDX  (archive.org, always reachable)")
        print(f"{'='*62}")
        for pattern, folder in tqdm(WB_PATTERNS, desc="Wayback", unit="pattern"):
            hits = wayback_search(pattern, limit=wb_limit)
            for original_url, wayback_url in hits:
                # Prefer Wayback URL (always available); fallback to original
                attempt(wayback_url, folder)
            time.sleep(1.0)

    # ── Source 2: Common Crawl ────────────────────────────────────────────────
    if "commoncrawl" in sources:
        print(f"\n{'='*62}")
        print("SOURCE 2 — Common Crawl CDX  (domain + keyword filter)")
        print(f"{'='*62}")
        for url_pattern, keyword, folder in tqdm(CC_PATTERNS, desc="CommonCrawl", unit="domain"):
            urls = cc_search(url_pattern, keyword, limit=cc_limit)
            for url in urls:
                attempt(url, folder)
            time.sleep(1.5)

    # ── Source 3: HTML page crawl ─────────────────────────────────────────────
    if "crawl" in sources:
        print(f"\n{'='*62}")
        print("SOURCE 3 — Crawling insurer provider portal pages")
        print(f"{'='*62}")
        for page_url, folder in tqdm(CRAWL_PAGES, desc="Crawling", unit="page"):
            pdf_links = crawl_page(page_url)
            for url in pdf_links:
                attempt(url, folder)
            time.sleep(2.0)

    # ── Source 4: SearXNG (Google + Bing dorks via proxy) ────────────────────
    if "searx" in sources:
        print(f"\n{'='*62}")
        print("SOURCE 4 — SearXNG  (Google + Bing dorks, no API key)")
        print(f"{'='*62}")
        instance = get_searx_instance()
        if not instance:
            print("  No reachable SearXNG instance — skipping this source.")
        else:
            print(f"  Using instance: {instance}\n")
            for query, folder in tqdm(GOOGLE_DORKS, desc="SearXNG", unit="query"):
                urls = searx_search(query, max_results=searx_results)
                for url in urls:
                    attempt(url, folder)
                time.sleep(random.uniform(3.0, 5.0))

    # ── Source 5: Google Custom Search API ───────────────────────────────────
    if "google" in sources and google_key and google_cx:
        print(f"\n{'='*62}")
        print("SOURCE 5 — Google Custom Search API  (100 free queries/day)")
        print(f"{'='*62}\n")
        for query, folder in tqdm(GOOGLE_DORKS, desc="Google CSE", unit="query"):
            urls = google_cse_search(query, google_key, google_cx)
            for url in urls:
                attempt(url, folder)
            time.sleep(1.5)
    elif "google" in sources and not (google_key and google_cx):
        print("\nGoogle CSE: --google-key and --google-cx not provided — skipping.")

    # ── Summary ───────────────────────────────────────────────────────────────
    log_file = base_dir / "download_log.txt"
    with open(log_file, "w") as f:
        f.write(f"Fillable Prior Auth PDF Download Log\n")
        f.write(f"Date   : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Sources: {', '.join(sorted(sources))}\n")
        f.write(f"Folder : {base_dir.resolve()}\n\n")
        f.write(f"Tried  : {tried}\n")
        f.write(f"Saved  : {saved}\n\n")
        f.write("Rejection reasons:\n")
        for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
            f.write(f"  {c:4d}x  {r}\n")

    print(f"\n{'='*62}")
    print(f"Done.  {saved} fillable PDFs saved → {base_dir.resolve()}")
    print(f"       (tried {tried}, rejected {tried - saved})")
    if reasons:
        print("\nRejection breakdown:")
        for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {c:4d}x  {r}")
    print(f"\nLog: {log_file}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download fillable prior auth PDFs — no API key required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--output", "-o",
        default=str(Path.home() / "prior_auth_forms"),
        help="Output folder (default: ~/prior_auth_forms)")
    parser.add_argument("--sources",
        default="searx,wayback,commoncrawl,crawl",
        help="Comma-separated sources: searx,wayback,commoncrawl,crawl,google (default: all except google)")
    parser.add_argument("--wb-limit", type=int, default=100,
        help="Max Wayback CDX results per pattern (default: 100)")
    parser.add_argument("--cc-limit", type=int, default=200,
        help="Max Common Crawl CDX results per domain (default: 200)")
    parser.add_argument("--download-delay", type=float, default=1.0,
        help="Seconds between downloads (default: 1.0)")
    parser.add_argument("--searx-results", type=int, default=30,
        help="Max PDF results per SearXNG query (default: 30)")
    parser.add_argument("--google-key",
        default=os.environ.get("GOOGLE_CSE_API_KEY", ""),
        help="Google Custom Search API key (or set GOOGLE_CSE_API_KEY env var)")
    parser.add_argument("--google-cx",
        default=os.environ.get("GOOGLE_CSE_CX", ""),
        help="Google Custom Search Engine ID (or set GOOGLE_CSE_CX env var)")
    parser.add_argument("--verbose", "-v", action="store_true",
        help="Show debug logs including skip reasons")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if not HAS_PYPDF:
        log.warning("pypdf not installed — fillable check disabled. Run: pip install pypdf")

    sources = {s.strip().lower() for s in args.sources.split(",")}
    valid = {"wayback", "commoncrawl", "crawl", "searx", "google"}
    bad = sources - valid
    if bad:
        parser.error(f"Unknown source(s): {bad}. Valid: {valid}")

    if "google" in sources and not (args.google_key and args.google_cx):
        print("NOTE: 'google' source requires --google-key and --google-cx (or env vars).")
        print("      See docstring for free setup instructions.\n")

    run(
        base_dir=Path(args.output),
        sources=sources,
        wb_limit=args.wb_limit,
        cc_limit=args.cc_limit,
        download_delay=args.download_delay,
        google_key=args.google_key,
        google_cx=args.google_cx,
        searx_results=args.searx_results,
    )


if __name__ == "__main__":
    main()
