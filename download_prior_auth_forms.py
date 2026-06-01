#!/usr/bin/env python3
"""
Prior Authorization Form Downloader
Searches for publicly available prior auth PDF forms using DuckDuckGo
and downloads them into organized local folders.

Requirements:
    pip install duckduckgo-search requests tqdm
"""

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

try:
    from duckduckgo_search import DDGS
except ImportError:
    print("Missing dependency. Run: pip install duckduckgo-search requests tqdm")
    raise

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Search queries grouped by category ───────────────────────────────────────
QUERIES: dict[str, list[str]] = {
    "general_fillable": [
        'filetype:pdf "prior authorization form" fillable',
        'filetype:pdf "prior authorization request" form',
        'filetype:pdf "prior authorization" "specialty pharmacy" fillable',
        'filetype:pdf "step therapy" "prior authorization form"',
    ],
    "major_insurers": [
        'filetype:pdf "prior authorization form" site:uhc.com',
        'filetype:pdf "prior authorization form" site:cigna.com',
        'filetype:pdf "prior authorization form" site:aetna.com',
        'filetype:pdf "prior authorization form" site:anthem.com',
        'filetype:pdf "prior authorization form" site:bcbs.com',
    ],
    "specialty_therapy": [
        'filetype:pdf "prior authorization" "GLP-1" form',
        'filetype:pdf "prior authorization" "biologics" fillable',
        'filetype:pdf "prior authorization" "oncology" request form',
        'filetype:pdf "prior authorization" "rheumatology" fillable',
    ],
    "adobe_livecycle": [
        'filetype:pdf "prior authorization" "Adobe LiveCycle"',
    ],
}

# ── HTTP session ──────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})

_seen_urls: set[str] = set()
_seen_hashes: set[str] = set()


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = unquote(name)
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:max_len]


def url_to_filename(url: str) -> str:
    parsed = urlparse(url)
    raw = os.path.basename(parsed.path) or "form"
    if not raw.lower().endswith(".pdf"):
        raw += ".pdf"
    domain = parsed.netloc.replace("www.", "").split(".")[0]
    return sanitize_filename(f"{domain}_{raw}")


def download_pdf(url: str, dest_dir: Path, timeout: int = 30) -> bool:
    if url in _seen_urls:
        log.debug("Skip duplicate URL: %s", url)
        return False
    _seen_urls.add(url)

    filename = url_to_filename(url)
    dest_path = dest_dir / filename

    try:
        resp = SESSION.get(url, timeout=timeout, stream=True, allow_redirects=True)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
            # Peek at first bytes to verify PDF magic number
            first_bytes = b""
            for chunk in resp.iter_content(4):
                first_bytes = chunk
                break
            if not first_bytes.startswith(b"%PDF"):
                log.warning("Not a PDF (skipping): %s", url)
                return False
            # Re-open if we consumed bytes — just restart
            resp.close()
            resp = SESSION.get(url, timeout=timeout, stream=True, allow_redirects=True)
            resp.raise_for_status()

        data = b""
        for chunk in resp.iter_content(chunk_size=8192):
            data += chunk

        file_hash = hashlib.md5(data).hexdigest()
        if file_hash in _seen_hashes:
            log.info("Skip duplicate content: %s", url)
            return False
        _seen_hashes.add(file_hash)

        # Handle filename collisions
        if dest_path.exists():
            dest_path = dest_dir / f"{dest_path.stem}_{file_hash[:6]}.pdf"

        dest_path.write_bytes(data)
        log.info("  Saved  %-60s  (%d KB)", dest_path.name, len(data) // 1024)
        return True

    except requests.exceptions.RequestException as exc:
        log.warning("  Download failed [%s]: %s", type(exc).__name__, url)
        return False


def search_and_download(
    query: str,
    dest_dir: Path,
    max_results: int = 20,
    delay: float = 2.0,
) -> int:
    log.info("Searching: %s", query)
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        log.warning("Search error for query '%s': %s", query, exc)
        return 0

    pdf_results = [r for r in results if r.get("href", "").lower().endswith(".pdf")]
    if not pdf_results:
        # Fall back to all results — some PDF URLs don't end with .pdf
        pdf_results = results

    log.info("  Found %d results", len(pdf_results))

    for result in pdf_results:
        url = result.get("href", "")
        if not url or not url.startswith("http"):
            continue
        if download_pdf(url, dest_dir):
            downloaded += 1
        time.sleep(delay)

    return downloaded


def run(
    base_dir: Path,
    max_results: int = 15,
    search_delay: float = 3.0,
    download_delay: float = 1.5,
) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    log_file = base_dir / "download_log.txt"

    total = 0
    summary: list[str] = []

    for category, queries in QUERIES.items():
        cat_dir = base_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        cat_count = 0

        print(f"\n{'='*60}")
        print(f"Category: {category.upper().replace('_', ' ')}")
        print(f"{'='*60}")

        for query in tqdm(queries, desc=category, unit="query"):
            count = search_and_download(
                query=query,
                dest_dir=cat_dir,
                max_results=max_results,
                delay=download_delay,
            )
            cat_count += count
            time.sleep(search_delay)

        total += cat_count
        summary.append(f"{category}: {cat_count} files")
        log.info("Category '%s' done — %d files downloaded", category, cat_count)

    # Write summary log
    with open(log_file, "w") as f:
        f.write("Prior Authorization Form Download Summary\n")
        f.write(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Output folder: {base_dir.resolve()}\n\n")
        for line in summary:
            f.write(f"  {line}\n")
        f.write(f"\nTotal downloaded: {total}\n")
        f.write(f"Unique URLs seen: {len(_seen_urls)}\n")

    print(f"\n{'='*60}")
    print(f"Done. {total} PDFs saved to: {base_dir.resolve()}")
    for line in summary:
        print(f"  {line}")
    print(f"\nLog: {log_file}")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download prior authorization PDF forms via DuckDuckGo search."
    )
    parser.add_argument(
        "--output", "-o",
        default=str(Path.home() / "prior_auth_forms"),
        help="Root folder for downloads (default: ~/prior_auth_forms)",
    )
    parser.add_argument(
        "--max-results", "-n",
        type=int, default=15,
        help="Max search results per query (default: 15)",
    )
    parser.add_argument(
        "--search-delay",
        type=float, default=3.0,
        help="Seconds between search requests (default: 3.0)",
    )
    parser.add_argument(
        "--download-delay",
        type=float, default=1.5,
        help="Seconds between download requests (default: 1.5)",
    )
    args = parser.parse_args()

    run(
        base_dir=Path(args.output),
        max_results=args.max_results,
        search_delay=args.search_delay,
        download_delay=args.download_delay,
    )


if __name__ == "__main__":
    main()
