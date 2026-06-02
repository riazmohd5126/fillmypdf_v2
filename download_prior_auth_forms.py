#!/usr/bin/env python3
"""
Prior Authorization Form Downloader  —  v3
Searches for publicly available prior-auth PDF forms using three search
backends (DuckDuckGo, Bing Web Search API, Google Custom Search API)
and crawls known insurer pages directly.

Requirements:
    pip install duckduckgo-search requests tqdm beautifulsoup4 lxml

Optional (strongly recommended for higher yield):
    Bing Web Search API key  → https://azure.microsoft.com/en-us/products/ai-services/bing-search
    Google CSE key + CX      → https://programmablesearchengine.google.com/

Usage examples:
    # No API keys — DDG only (moderate yield)
    python download_prior_auth_forms.py

    # With Bing (best free-tier option — 1,000 calls/month free)
    python download_prior_auth_forms.py --bing-key YOUR_KEY

    # With Google CSE (100 free queries/day)
    python download_prior_auth_forms.py --google-key YOUR_KEY --google-cx YOUR_CX

    # All three backends + custom output folder
    python download_prior_auth_forms.py \\
        --bing-key YOUR_KEY \\
        --google-key YOUR_KEY --google-cx YOUR_CX \\
        --output ~/Desktop/prior_auth_pdfs \\
        --verbose
"""

import os
import re
import time
import hashlib
import logging
import argparse
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

try:
    from duckduckgo_search import DDGS
    HAS_DDG = True
except ImportError:
    HAS_DDG = False

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Dork catalogue  (7 categories from spec)
# filetype:pdf is kept for Bing/Google; DDG gets simplified versions
# ─────────────────────────────────────────────────────────────────────────────

# Each entry: (ddg_query, bing_or_google_query, category_folder)
DORKS: list[tuple[str, str, str]] = [

    # ── Dork 1: Generic PA Forms ──────────────────────────────────────────────
    (
        '"prior authorization" OR "PA request" OR "authorization request" fillable pdf',
        'filetype:pdf ("prior authorization" OR "PA request" OR "authorization request")',
        "01_generic_pa",
    ),

    # ── Dork 2: Medication PA Forms ───────────────────────────────────────────
    (
        '"prior authorization" ("patient" OR "member") ("prescriber" OR "provider" OR "physician") pdf',
        'filetype:pdf "prior authorization" ("patient" OR "member") ("prescriber" OR "provider" OR "physician")',
        "02_medication_pa",
    ),

    # ── Dork 3: Procedure / Medical PA Forms ──────────────────────────────────
    (
        '"prior authorization" ("procedure code" OR "CPT") pdf',
        'filetype:pdf "prior authorization" ("procedure code" OR "CPT")',
        "03_procedure_pa",
    ),

    # ── Dork 4: Specialty Drug Forms ──────────────────────────────────────────
    (
        '"prior authorization" ("drug name" OR "medication") fillable pdf',
        'filetype:pdf "prior authorization" ("drug name" OR "medication")',
        "04_specialty_drug",
    ),

    # ── Dork 5: Fax-Based Forms ───────────────────────────────────────────────
    (
        '"prior authorization" "fax" fillable form pdf',
        'filetype:pdf "prior authorization" "fax"',
        "05_fax_forms",
    ),

    # ── Dork 7: NLP Training Gold Mine ────────────────────────────────────────
    (
        '"patient name" "date of birth" "prescriber" "fax" "prior authorization" pdf',
        'filetype:pdf "patient name" "date of birth" "prescriber" "fax"',
        "07_nlp_training",
    ),
]

# ── Dork 6: Insurer-specific site: searches ───────────────────────────────────
INSURER_DORKS: list[tuple[str, str, str]] = [
    (
        'site:aetna.com "prior authorization" pdf',
        'site:aetna.com filetype:pdf "prior authorization"',
        "06_insurers/aetna",
    ),
    (
        'site:caremark.com "prior authorization" pdf',
        'site:caremark.com filetype:pdf "prior authorization"',
        "06_insurers/caremark",
    ),
    (
        'site:optumrx.com "prior authorization" pdf',
        'site:optumrx.com filetype:pdf "prior authorization"',
        "06_insurers/optumrx",
    ),
    (
        'site:express-scripts.com "prior authorization" pdf',
        'site:express-scripts.com filetype:pdf "prior authorization"',
        "06_insurers/express_scripts",
    ),
    (
        'site:cigna.com "prior authorization" pdf',
        'site:cigna.com filetype:pdf "prior authorization"',
        "06_insurers/cigna",
    ),
    (
        'site:bcbs.com "prior authorization" pdf',
        'site:bcbs.com filetype:pdf "prior authorization"',
        "06_insurers/bcbs",
    ),
    (
        'site:uhc.com "prior authorization" pdf',
        'site:uhc.com filetype:pdf "prior authorization"',
        "06_insurers/uhc",
    ),
    (
        'site:anthem.com "prior authorization" pdf',
        'site:anthem.com filetype:pdf "prior authorization"',
        "06_insurers/anthem",
    ),
    (
        'site:humana.com "prior authorization" pdf',
        'site:humana.com filetype:pdf "prior authorization"',
        "06_insurers/humana",
    ),
]

ALL_DORKS = DORKS + INSURER_DORKS

# ── Known insurer landing pages to crawl directly ────────────────────────────
SEED_PAGES: list[tuple[str, str]] = [
    ("https://www.uhcprovider.com/en/prior-auth-advance-notification.html",      "06_insurers/uhc"),
    ("https://www.aetna.com/health-care-professionals/prior-authorization-overview.html", "06_insurers/aetna"),
    ("https://www.cigna.com/healthcare-providers/coverage-and-claims/prior-authorization", "06_insurers/cigna"),
    ("https://www.anthem.com/provider/prior-authorization/",                     "06_insurers/anthem"),
    ("https://www.caremark.com/portal/asset/PriorAuthorizationForms.pdf",        "06_insurers/caremark"),
    ("https://www.optumrx.com/oe/sca/prior-authorization",                       "06_insurers/optumrx"),
    ("https://www.humana.com/provider/medical-resources/prior-authorization",     "06_insurers/humana"),
    ("https://www.bcbsil.com/provider/clinical/prior_authorization.html",         "06_insurers/bcbs"),
    ("https://www.bcbstx.com/provider/clinical/prior_authorization.html",         "06_insurers/bcbs"),
]

# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
})

_seen_urls: set[str] = set()
_seen_hashes: set[str] = set()


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = unquote(name)
    name = re.sub(r'[\\/*?:"<>|\']', "_", name)
    name = re.sub(r"\s+", "_", name.strip())
    return re.sub(r"_+", "_", name)[:max_len] or "form"


def url_to_filename(url: str) -> str:
    parsed = urlparse(url)
    raw = os.path.basename(parsed.path.rstrip("/")) or "form"
    if not raw.lower().endswith(".pdf"):
        raw += ".pdf"
    domain = parsed.netloc.replace("www.", "").split(".")[0]
    return sanitize_filename(f"{domain}_{raw}")


def looks_like_pdf(url: str) -> bool:
    lower = url.lower()
    return ".pdf" in lower


def download_pdf(url: str, dest_dir: Path, timeout: int = 30) -> bool:
    """Fetch URL; save only if valid PDF magic bytes found. Returns True on save."""
    url = url.split("#")[0].strip()
    if url in _seen_urls:
        return False
    _seen_urls.add(url)

    try:
        resp = SESSION.get(url, timeout=timeout, stream=True, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.debug("Fetch failed (%s): %s", type(exc).__name__, url)
        return False

    head = b""
    for chunk in resp.iter_content(2048):
        head = chunk
        break

    if not head.startswith(b"%PDF"):
        log.debug("Not a PDF (bad magic): %s", url)
        resp.close()
        return False

    try:
        data = head + b"".join(resp.iter_content(8192))
    except requests.exceptions.RequestException:
        return False

    file_hash = hashlib.md5(data).hexdigest()
    if file_hash in _seen_hashes:
        log.debug("Duplicate content hash: %s", url)
        return False
    _seen_hashes.add(file_hash)

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / url_to_filename(url)
    if dest_path.exists():
        dest_path = dest_dir / f"{dest_path.stem}_{file_hash[:6]}.pdf"

    dest_path.write_bytes(data)
    log.info("  SAVED  %-65s  %d KB", dest_path.name, len(data) // 1024)
    return True


def try_download_urls(
    urls: list[str],
    dest_dir: Path,
    delay: float,
    label: str = "",
) -> int:
    pdf_urls = [u for u in urls if u.startswith("http") and looks_like_pdf(u)]
    all_urls = [u for u in urls if u.startswith("http") and not looks_like_pdf(u)]
    candidates = pdf_urls + all_urls  # try PDF-looking ones first

    log.info("  [%s] %d candidate URLs (%d PDF-looking)", label, len(candidates), len(pdf_urls))
    saved = 0
    for url in candidates:
        if download_pdf(url, dest_dir):
            saved += 1
        time.sleep(delay)
    return saved


# ─────────────────────────────────────────────────────────────────────────────
# Search backends
# ─────────────────────────────────────────────────────────────────────────────

def search_ddg(query: str, max_results: int) -> list[str]:
    if not HAS_DDG:
        return []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        urls = [r.get("href", "") for r in results if r.get("href")]
        log.info("  DDG  '%s'  → %d results", query[:70], len(urls))
        return urls
    except Exception as exc:
        log.warning("  DDG error: %s", exc)
        return []


def search_bing(query: str, api_key: str, max_results: int = 50) -> list[str]:
    """
    Bing Web Search API v7.
    Supports filetype:pdf natively in the query string.
    Free tier: 1,000 transactions/month at https://aka.ms/bingapisignup
    """
    endpoint = "https://api.bing.microsoft.com/v7.0/search"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    urls: list[str] = []
    offset = 0
    per_page = min(50, max_results)

    while offset < max_results:
        params = {
            "q": query,
            "count": per_page,
            "offset": offset,
            "mkt": "en-US",
            "responseFilter": "Webpages",
        }
        try:
            resp = SESSION.get(endpoint, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            pages = data.get("webPages", {}).get("value", [])
            if not pages:
                break
            urls.extend(p["url"] for p in pages if p.get("url"))
            offset += len(pages)
            if len(pages) < per_page:
                break
        except Exception as exc:
            log.warning("  Bing error: %s", exc)
            break

    log.info("  Bing '%s'  → %d results", query[:70], len(urls))
    return urls


def search_google_cse(query: str, api_key: str, cx: str, max_results: int = 100) -> list[str]:
    """
    Google Custom Search JSON API.
    100 free queries/day; add fileType=pdf for native filtering.
    Sign up: https://programmablesearchengine.google.com/
    """
    endpoint = "https://www.googleapis.com/customsearch/v1"
    urls: list[str] = []
    start = 1  # Google CSE uses 1-based paging, max 100 results (10 pages of 10)

    # Extract filetype: from query if present, pass as fileType param
    filetype = None
    clean_query = query
    ft_match = re.search(r"filetype:(\w+)", query, re.IGNORECASE)
    if ft_match:
        filetype = ft_match.group(1)
        clean_query = query.replace(ft_match.group(0), "").strip()

    while start <= min(max_results, 91):  # Google CSE max = 10 pages × 10 results
        params: dict = {
            "key": api_key,
            "cx": cx,
            "q": clean_query,
            "num": 10,
            "start": start,
        }
        if filetype:
            params["fileType"] = filetype

        try:
            resp = SESSION.get(endpoint, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            if not items:
                break
            urls.extend(item["link"] for item in items if item.get("link"))
            start += len(items)
            if len(items) < 10:
                break
        except Exception as exc:
            log.warning("  Google CSE error: %s", exc)
            break

    log.info("  Google CSE '%s'  → %d results", query[:70], len(urls))
    return urls


# ─────────────────────────────────────────────────────────────────────────────
# Page crawler
# ─────────────────────────────────────────────────────────────────────────────

def crawl_page_for_pdfs(page_url: str) -> list[str]:
    try:
        resp = SESSION.get(page_url, timeout=20, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.debug("Seed page failed (%s): %s", type(exc).__name__, page_url)
        return []

    content_type = resp.headers.get("content-type", "")
    # If the seed page IS a PDF, return it directly
    if "pdf" in content_type.lower() or page_url.lower().endswith(".pdf"):
        return [page_url]

    soup = BeautifulSoup(resp.text, "lxml")
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith("#"):
            continue
        full = urljoin(page_url, href)
        if full.lower().endswith(".pdf") or "/pdf" in full.lower():
            links.append(full)

    log.info("  Crawled %-60s → %d PDF links", page_url, len(links))
    return links


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run(
    base_dir: Path,
    max_results: int,
    search_delay: float,
    download_delay: float,
    bing_key: str | None,
    google_key: str | None,
    google_cx: str | None,
) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    summary: list[str] = []

    backends = ["DDG"]
    if bing_key:
        backends.append("Bing")
    if google_key and google_cx:
        backends.append("Google CSE")
    print(f"\nActive search backends: {', '.join(backends)}")

    # ── Phase 1: Search all dorks ─────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("PHASE 1 — Keyword searches (7 dork categories)")
    print(f"{'='*65}")

    for ddg_q, boog_q, folder in tqdm(ALL_DORKS, desc="dorks", unit="query"):
        dest_dir = base_dir / folder
        cat_count = 0
        all_urls: list[str] = []

        all_urls += search_ddg(ddg_q, max_results)
        time.sleep(search_delay)

        if bing_key:
            all_urls += search_bing(boog_q, bing_key, max_results)
            time.sleep(search_delay)

        if google_key and google_cx:
            all_urls += search_google_cse(boog_q, google_key, google_cx, max_results)
            time.sleep(search_delay)

        cat_count += try_download_urls(all_urls, dest_dir, download_delay, folder)
        total += cat_count
        summary.append(f"{folder}: {cat_count} files")

    # ── Phase 2: Crawl insurer landing pages ──────────────────────────────────
    print(f"\n{'='*65}")
    print("PHASE 2 — Crawling insurer landing pages directly")
    print(f"{'='*65}")

    for page_url, folder in tqdm(SEED_PAGES, desc="crawl", unit="page"):
        dest_dir = base_dir / folder
        pdf_links = crawl_page_for_pdfs(page_url)
        count = try_download_urls(pdf_links, dest_dir, download_delay, folder)
        total += count
        summary.append(f"crawl:{folder}: {count} files")
        time.sleep(download_delay)

    # ── Summary ───────────────────────────────────────────────────────────────
    log_file = base_dir / "download_log.txt"
    with open(log_file, "w") as f:
        f.write("Prior Authorization Form Download Log\n")
        f.write(f"Date    : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Backends: {', '.join(backends)}\n")
        f.write(f"Folder  : {base_dir.resolve()}\n\n")
        for line in summary:
            f.write(f"  {line}\n")
        f.write(f"\nTotal PDFs saved : {total}\n")
        f.write(f"Unique URLs tried: {len(_seen_urls)}\n")

    print(f"\n{'='*65}")
    print(f"Done.  {total} PDFs saved  →  {base_dir.resolve()}")
    non_zero = [s for s in summary if not s.endswith(": 0 files")]
    for line in non_zero or summary[:10]:
        print(f"  {line}")
    print(f"\nFull log: {log_file}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download prior authorization PDF forms.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--output", "-o",
        default=str(Path.home() / "prior_auth_forms"),
        help="Root output folder (default: ~/prior_auth_forms)")
    parser.add_argument("--max-results", "-n", type=int, default=20,
        help="Max results per query per backend (default: 20)")
    parser.add_argument("--search-delay", type=float, default=4.0,
        help="Seconds between search requests (default: 4.0)")
    parser.add_argument("--download-delay", type=float, default=1.0,
        help="Seconds between PDF downloads (default: 1.0)")

    # API keys (optional but strongly recommended)
    parser.add_argument("--bing-key",
        default=os.environ.get("BING_SEARCH_API_KEY"),
        help="Bing Web Search API key (or set BING_SEARCH_API_KEY env var)")
    parser.add_argument("--google-key",
        default=os.environ.get("GOOGLE_CSE_API_KEY"),
        help="Google Custom Search API key (or set GOOGLE_CSE_API_KEY env var)")
    parser.add_argument("--google-cx",
        default=os.environ.get("GOOGLE_CSE_CX"),
        help="Google Custom Search Engine ID (or set GOOGLE_CSE_CX env var)")

    parser.add_argument("--verbose", "-v", action="store_true",
        help="Show debug-level logs")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if not HAS_DDG:
        log.warning("duckduckgo-search not installed — DDG backend disabled")

    run(
        base_dir=Path(args.output),
        max_results=args.max_results,
        search_delay=args.search_delay,
        download_delay=args.download_delay,
        bing_key=args.bing_key,
        google_key=args.google_key,
        google_cx=args.google_cx,
    )


if __name__ == "__main__":
    main()
