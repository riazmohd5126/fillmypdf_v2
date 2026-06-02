#!/usr/bin/env python3
"""
Prior Authorization PDF Downloader — Common Crawl CDX Edition
100% free, no API key, no account, no credit card.

How it works:
  1. Queries Common Crawl's CDX index for PDF URLs on known insurer domains
  2. Filters results to prior-auth-related filenames/paths
  3. Downloads the PDFs directly from the original insurer websites

Requirements:
    pip install requests tqdm

Usage:
    python commoncrawl_prior_auth.py
    python commoncrawl_prior_auth.py --output ~/Desktop/pa_forms --verbose
"""

import json
import os
import re
import time
import hashlib
import logging
import argparse
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from tqdm import tqdm

log = logging.getLogger(__name__)

# ── Insurer domains to search in Common Crawl ────────────────────────────────
# Format: (url_pattern, label)
# The * wildcard matches any subdomain or path segment
DOMAIN_PATTERNS: list[tuple[str, str]] = [
    ("*.uhc.com/*.pdf",               "uhc"),
    ("*.uhcprovider.com/*.pdf",        "uhc"),
    ("*.aetna.com/*.pdf",              "aetna"),
    ("*.cigna.com/*.pdf",             "cigna"),
    ("*.anthem.com/*.pdf",            "anthem"),
    ("*.bcbs.com/*.pdf",              "bcbs"),
    ("*.bcbsil.com/*.pdf",            "bcbs"),
    ("*.bcbstx.com/*.pdf",            "bcbs"),
    ("*.bcbsma.com/*.pdf",            "bcbs"),
    ("*.humana.com/*.pdf",            "humana"),
    ("*.caremark.com/*.pdf",          "caremark"),
    ("*.optumrx.com/*.pdf",           "optum"),
    ("*.optum.com/*.pdf",             "optum"),
    ("*.magellanhealth.com/*.pdf",    "magellan"),
    ("*.coventry.com/*.pdf",          "coventry"),
    ("*.centene.com/*.pdf",           "centene"),
    ("*.molina.com/*.pdf",            "molina"),
    ("*.wellcare.com/*.pdf",          "wellcare"),
    ("*.express-scripts.com/*.pdf",   "express_scripts"),
    ("*.evernorth.com/*.pdf",         "evernorth"),
]

# Keywords that must appear in the URL path to be considered a prior-auth form.
# Keeps out unrelated PDFs like annual reports, marketing brochures, etc.
PRIOR_AUTH_KEYWORDS = [
    "prior-auth", "priorauth", "prior_auth",
    "authorization", "authoriz",
    "prior-approval", "priorapproval",
    "pa-form", "paform", "pa_form",
    "precert", "pre-cert",
    "step-therapy", "steptherapy",
    "specialty-pharmacy", "specialtypharmacy",
    "formulary",
    "utilization",
    "medical-necessity",
]

# ── HTTP session ──────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})

_seen_hashes: set[str] = set()
_seen_urls:   set[str] = set()


# ── Common Crawl helpers ──────────────────────────────────────────────────────

def get_latest_crawl_id() -> str:
    """Return the ID of the most recent Common Crawl snapshot."""
    resp = SESSION.get(
        "https://index.commoncrawl.org/collinfo.json",
        timeout=20,
    )
    resp.raise_for_status()
    crawls = resp.json()
    crawl_id = crawls[0]["id"]          # newest is first
    log.info("Using Common Crawl snapshot: %s", crawl_id)
    return crawl_id


def cdx_search(
    crawl_id: str,
    url_pattern: str,
    limit: int = 500,
) -> list[str]:
    """
    Query the CDX index for one URL pattern.
    Returns a list of original URLs (not CC URLs).

    CDX API docs: https://github.com/webrecorder/pywb/wiki/CDX-Server-API
    """
    endpoint = f"https://index.commoncrawl.org/{crawl_id}-index"
    params = {
        "url":    url_pattern,
        "output": "json",
        "filter": "mime:application/pdf",   # only PDFs
        "fl":     "url,status",             # fields we need
        "limit":  limit,
        "collapse": "urlkey",               # deduplicate by URL
    }

    try:
        resp = SESSION.get(endpoint, params=params, timeout=60)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.warning("CDX query failed for %s: %s", url_pattern, exc)
        return []

    urls = []
    for line in resp.text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") != "200":
            continue
        url = record.get("url", "")
        if url:
            urls.append(url)

    log.info("  CDX  %-40s  → %d URLs", url_pattern, len(urls))
    return urls


def is_prior_auth_url(url: str) -> bool:
    """Return True if the URL path suggests a prior-auth form."""
    lower = url.lower()
    return any(kw in lower for kw in PRIOR_AUTH_KEYWORDS)


# ── Download helper ───────────────────────────────────────────────────────────

def sanitize(name: str, maxlen: int = 100) -> str:
    name = unquote(name)
    name = re.sub(r'[\\/*?:"<>|\']+', "_", name)
    name = re.sub(r"\s+", "_", name.strip())
    return re.sub(r"_+", "_", name)[:maxlen] or "form"


def url_to_filename(url: str, label: str) -> str:
    parsed = urlparse(url)
    base = os.path.basename(parsed.path) or "form"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return sanitize(f"{label}_{base}")


def download_pdf(url: str, dest_dir: Path, label: str, timeout: int = 30) -> bool:
    if url in _seen_urls:
        return False
    _seen_urls.add(url)

    try:
        resp = SESSION.get(url, timeout=timeout, stream=True, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.debug("Download failed (%s): %s", type(exc).__name__, url)
        return False

    # Read first bytes and verify PDF magic
    head = b""
    for chunk in resp.iter_content(2048):
        head = chunk
        break

    if not head.startswith(b"%PDF"):
        log.debug("Not a PDF: %s", url)
        resp.close()
        return False

    try:
        data = head + b"".join(resp.iter_content(8192))
    except requests.exceptions.RequestException:
        return False

    file_hash = hashlib.md5(data).hexdigest()
    if file_hash in _seen_hashes:
        log.debug("Duplicate content: %s", url)
        return False
    _seen_hashes.add(file_hash)

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / url_to_filename(url, label)
    if dest_path.exists():
        dest_path = dest_dir / f"{dest_path.stem}_{file_hash[:6]}.pdf"

    dest_path.write_bytes(data)
    log.info("    SAVED  %-60s  %d KB", dest_path.name, len(data) // 1024)
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def run(
    base_dir: Path,
    cdx_limit: int,
    download_delay: float,
    filter_keywords: bool,
) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)

    print("\nStep 1 — fetching latest Common Crawl snapshot ID...")
    try:
        crawl_id = get_latest_crawl_id()
    except Exception as exc:
        print(f"ERROR: Could not reach Common Crawl: {exc}")
        return

    total_found  = 0
    total_saved  = 0
    summary: list[str] = []

    print(f"\nStep 2 — querying CDX index for {len(DOMAIN_PATTERNS)} domain patterns...\n")

    for url_pattern, label in tqdm(DOMAIN_PATTERNS, desc="CDX queries", unit="domain"):
        # ── Query Common Crawl ────────────────────────────────────────────────
        all_urls = cdx_search(crawl_id, url_pattern, limit=cdx_limit)

        # ── Optionally filter to prior-auth-looking URLs ──────────────────────
        if filter_keywords:
            filtered = [u for u in all_urls if is_prior_auth_url(u)]
            log.info("    Keyword filter: %d → %d URLs", len(all_urls), len(filtered))
        else:
            filtered = all_urls

        total_found += len(filtered)

        # ── Download ──────────────────────────────────────────────────────────
        dest_dir = base_dir / label
        saved = 0
        for url in filtered:
            if download_pdf(url, dest_dir, label):
                saved += 1
            time.sleep(download_delay)

        total_saved += saved
        summary.append(f"{label:20s}  found={len(filtered):4d}  saved={saved}")

    # ── Write log ─────────────────────────────────────────────────────────────
    log_file = base_dir / "commoncrawl_log.txt"
    with open(log_file, "w") as f:
        f.write("Common Crawl Prior Auth Download Log\n")
        f.write(f"Crawl snapshot : {crawl_id}\n")
        f.write(f"Date           : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Output folder  : {base_dir.resolve()}\n\n")
        f.write(f"{'Domain':20s}  {'CDX hits':>10}  {'Saved':>7}\n")
        f.write("-" * 45 + "\n")
        for line in summary:
            f.write(f"  {line}\n")
        f.write(f"\nTotal CDX hits : {total_found}\n")
        f.write(f"Total saved    : {total_saved}\n")

    print(f"\n{'='*55}")
    print(f"Done.  {total_saved} PDFs saved → {base_dir.resolve()}")
    print(f"\n{'Domain':20s}  {'CDX hits':>10}  {'Saved':>7}")
    print("-" * 45)
    for line in summary:
        print(f"  {line}")
    print(f"\nLog: {log_file}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download prior auth PDFs via Common Crawl CDX API (free, no key).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--output", "-o",
        default=str(Path.home() / "prior_auth_forms"),
        help="Output folder (default: ~/prior_auth_forms)")
    parser.add_argument("--limit", "-n", type=int, default=500,
        help="Max CDX results per domain (default: 500)")
    parser.add_argument("--download-delay", type=float, default=1.5,
        help="Seconds between downloads (default: 1.5)")
    parser.add_argument("--no-filter", action="store_true",
        help="Download ALL PDFs found, not just prior-auth-looking ones")
    parser.add_argument("--verbose", "-v", action="store_true",
        help="Show debug logs")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    run(
        base_dir=Path(args.output),
        cdx_limit=args.limit,
        download_delay=args.download_delay,
        filter_keywords=not args.no_filter,
    )


if __name__ == "__main__":
    main()
