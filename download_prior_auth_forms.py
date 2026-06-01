#!/usr/bin/env python3
"""
Prior Authorization Form Downloader
Searches for publicly available prior auth PDF forms and downloads them.

Strategy:
  1. Search DuckDuckGo with simplified queries, filter results for PDF links
  2. Crawl known insurer prior-auth landing pages to harvest PDF links
  3. Download and deduplicate everything into organized local folders

Requirements:
    pip install duckduckgo-search requests tqdm beautifulsoup4 lxml
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
from tqdm import tqdm
from bs4 import BeautifulSoup

try:
    from duckduckgo_search import DDGS
except ImportError:
    print("Missing dependency. Run: pip install duckduckgo-search requests tqdm beautifulsoup4 lxml")
    raise

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── DDG search queries (no filetype: — DDG ignores it) ───────────────────────
# We search broadly, then filter for PDF URLs in the results.
QUERIES: dict[str, list[str]] = {
    "general_fillable": [
        '"prior authorization form" fillable pdf',
        '"prior authorization request" fillable form pdf',
        '"prior authorization" "specialty pharmacy" fillable pdf download',
        '"step therapy" "prior authorization form" pdf',
    ],
    "major_insurers": [
        'site:uhc.com "prior authorization" form pdf',
        'site:cigna.com "prior authorization" form pdf',
        'site:aetna.com "prior authorization" form pdf',
        'site:anthem.com "prior authorization" form pdf',
        'site:bcbsfl.com OR site:bcbs.com "prior authorization" form pdf',
    ],
    "specialty_therapy": [
        '"prior authorization" "GLP-1" form pdf fillable',
        '"prior authorization" biologics fillable pdf',
        '"prior authorization" oncology "request form" pdf',
        '"prior authorization" rheumatology fillable pdf',
    ],
    "adobe_livecycle": [
        '"prior authorization" "Adobe LiveCycle" pdf fillable',
    ],
}

# ── Known insurer prior-auth landing pages to crawl for embedded PDF links ───
SEED_PAGES: dict[str, list[str]] = {
    "major_insurers": [
        "https://www.uhcprovider.com/en/prior-auth-advance-notification.html",
        "https://www.cigna.com/healthcare-providers/prior-authorization",
        "https://www.aetna.com/health-care-professionals/prior-authorization-overview.html",
        "https://www.anthem.com/provider/prior-authorization/",
        "https://www.bcbsil.com/provider/clinical/prior_authorization.html",
        "https://www.bcbstx.com/provider/clinical/prior_authorization.html",
        "https://www.humana.com/provider/medical-resources/prior-authorization",
        "https://www.centene.com/providers/prior-authorization.html",
    ],
    "specialty_therapy": [
        "https://www.nccn.org/docs/default-source/default-document-library/prior-authorization",
        "https://www.novomedlink.com/obesity/prior-authorization.html",
    ],
}

# ── Hardcoded high-value direct PDF URLs (known to exist) ────────────────────
DIRECT_URLS: dict[str, list[str]] = {
    "general_fillable": [
        "https://www.cms.gov/Medicare/CMS-Forms/CMS-Forms/downloads/cms10148.pdf",
        "https://www.cms.gov/medicare/medicare-fee-for-service-payment/physicianfeesched/downloads/mpapa.pdf",
    ],
    "major_insurers": [],
}

# ── HTTP session ──────────────────────────────────────────────────────────────
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
    name = re.sub(r"_+", "_", name)
    return name[:max_len] or "form"


def url_to_filename(url: str) -> str:
    parsed = urlparse(url)
    raw = os.path.basename(parsed.path.rstrip("/")) or "form"
    if not raw.lower().endswith(".pdf"):
        raw += ".pdf"
    domain = parsed.netloc.replace("www.", "").split(".")[0]
    return sanitize_filename(f"{domain}_{raw}")


def is_pdf_url(url: str) -> bool:
    """Quick heuristic check before fetching."""
    lower = url.lower()
    return ".pdf" in lower or "pdf" in lower


def download_pdf(url: str, dest_dir: Path, timeout: int = 30) -> bool:
    """Fetch a URL and save if it is a valid PDF. Returns True on success."""
    if url in _seen_urls:
        return False
    _seen_urls.add(url)

    try:
        resp = SESSION.get(url, timeout=timeout, stream=True, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.debug("Fetch failed (%s): %s", type(exc).__name__, url)
        return False

    # Read up to first 2 KB to verify PDF magic bytes
    head = b""
    for chunk in resp.iter_content(2048):
        head = chunk
        break

    if not head.startswith(b"%PDF"):
        log.debug("Not a PDF: %s", url)
        resp.close()
        return False

    # Read the rest
    try:
        data = head + b"".join(resp.iter_content(8192))
    except requests.exceptions.RequestException as exc:
        log.debug("Read failed (%s): %s", type(exc).__name__, url)
        return False

    file_hash = hashlib.md5(data).hexdigest()
    if file_hash in _seen_hashes:
        log.debug("Duplicate content: %s", url)
        return False
    _seen_hashes.add(file_hash)

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / url_to_filename(url)
    if dest_path.exists():
        dest_path = dest_dir / f"{dest_path.stem}_{file_hash[:6]}.pdf"

    dest_path.write_bytes(data)
    log.info("  SAVED  %s  (%d KB)", dest_path.name, len(data) // 1024)
    return True


def harvest_pdf_links_from_page(page_url: str) -> list[str]:
    """Load an HTML page and return all PDF href links found."""
    try:
        resp = SESSION.get(page_url, timeout=20, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.debug("Seed page failed (%s): %s", type(exc).__name__, page_url)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href:
            continue
        full = urljoin(page_url, href)
        if full.lower().endswith(".pdf") or "pdf" in full.lower():
            links.append(full)
    log.info("  Crawled %s → %d PDF links", page_url, len(links))
    return links


def ddg_search(query: str, max_results: int) -> list[str]:
    """Run a DDG text search and return all URLs found (PDF or not)."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        urls = [r.get("href", "") for r in results if r.get("href")]
        log.info("  DDG '%s' → %d results", query[:60], len(urls))
        return urls
    except Exception as exc:
        log.warning("  DDG search error: %s", exc)
        return []


def process_urls(urls: list[str], dest_dir: Path, delay: float) -> int:
    """Filter URLs for PDFs, download each, return count saved."""
    pdf_urls = [u for u in urls if u.startswith("http") and is_pdf_url(u)]
    log.info("  %d PDF-looking URLs to attempt", len(pdf_urls))
    saved = 0
    for url in pdf_urls:
        if download_pdf(url, dest_dir):
            saved += 1
        time.sleep(delay)
    return saved


def run(
    base_dir: Path,
    max_results: int = 20,
    search_delay: float = 4.0,
    download_delay: float = 1.0,
) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    summary: list[str] = []

    # ── Phase 1: DDG searches ─────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PHASE 1 — DuckDuckGo searches")
    print("=" * 60)

    for category, queries in QUERIES.items():
        cat_dir = base_dir / category
        cat_count = 0

        for query in tqdm(queries, desc=category, unit="query"):
            urls = ddg_search(query, max_results)
            cat_count += process_urls(urls, cat_dir, download_delay)
            time.sleep(search_delay)

        total += cat_count
        summary.append(f"{category} (search): {cat_count} files")

    # ── Phase 2: Crawl known insurer landing pages ───────────────────────────
    print("\n" + "=" * 60)
    print("PHASE 2 — Crawling insurer landing pages")
    print("=" * 60)

    for category, pages in SEED_PAGES.items():
        cat_dir = base_dir / category
        cat_count = 0

        for page_url in tqdm(pages, desc=f"crawl:{category}", unit="page"):
            pdf_links = harvest_pdf_links_from_page(page_url)
            cat_count += process_urls(pdf_links, cat_dir, download_delay)
            time.sleep(download_delay)

        total += cat_count
        summary.append(f"{category} (crawl): {cat_count} files")

    # ── Phase 3: Direct known URLs ────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PHASE 3 — Direct known PDF URLs")
    print("=" * 60)

    for category, urls in DIRECT_URLS.items():
        if not urls:
            continue
        cat_dir = base_dir / category
        cat_count = process_urls(urls, cat_dir, download_delay)
        total += cat_count
        summary.append(f"{category} (direct): {cat_count} files")

    # ── Summary ───────────────────────────────────────────────────────────────
    log_file = base_dir / "download_log.txt"
    with open(log_file, "w") as f:
        f.write("Prior Authorization Form Download Summary\n")
        f.write(f"Date  : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Folder: {base_dir.resolve()}\n\n")
        for line in summary:
            f.write(f"  {line}\n")
        f.write(f"\nTotal PDFs saved : {total}\n")
        f.write(f"Unique URLs tried: {len(_seen_urls)}\n")

    print(f"\n{'=' * 60}")
    print(f"Done. {total} PDFs saved to: {base_dir.resolve()}")
    for line in summary:
        print(f"  {line}")
    print(f"\nLog: {log_file}")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download prior authorization PDF forms."
    )
    parser.add_argument(
        "--output", "-o",
        default=str(Path.home() / "prior_auth_forms"),
        help="Root output folder (default: ~/prior_auth_forms)",
    )
    parser.add_argument(
        "--max-results", "-n",
        type=int, default=20,
        help="Max DDG results per query (default: 20)",
    )
    parser.add_argument(
        "--search-delay", type=float, default=4.0,
        help="Seconds between DDG searches (default: 4.0)",
    )
    parser.add_argument(
        "--download-delay", type=float, default=1.0,
        help="Seconds between PDF downloads (default: 1.0)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show debug-level logs",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    run(
        base_dir=Path(args.output),
        max_results=args.max_results,
        search_delay=args.search_delay,
        download_delay=args.download_delay,
    )


if __name__ == "__main__":
    main()
