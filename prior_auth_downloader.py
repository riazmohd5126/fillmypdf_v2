#!/usr/bin/env python3
"""
Prior Auth PDF Downloader
Runs Google dork searches and downloads found PDFs to ~/prior_auth_forms/

Install:
    pip install googlesearch-python requests tqdm

Run:
    python prior_auth_downloader.py
"""

import os, re, time, hashlib, logging
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from tqdm import tqdm
from googlesearch import search

# ── Output folder ─────────────────────────────────────────────────────────────
OUTPUT_DIR = Path.home() / "prior_auth_forms"

# ── Google dork queries → subfolder ──────────────────────────────────────────
QUERIES = [
    # Generic fillable forms
    ('filetype:pdf "prior authorization form" fillable',                "generic"),
    ('filetype:pdf "prior authorization request" form',                 "generic"),
    ('filetype:pdf "prior authorization" "specialty pharmacy" fillable',"generic"),
    ('filetype:pdf "step therapy" "prior authorization form"',          "generic"),

    # Major insurers
    ('filetype:pdf "prior authorization form" site:uhc.com',            "insurers/uhc"),
    ('filetype:pdf "prior authorization form" site:cigna.com',          "insurers/cigna"),
    ('filetype:pdf "prior authorization form" site:aetna.com',          "insurers/aetna"),
    ('filetype:pdf "prior authorization form" site:anthem.com',         "insurers/anthem"),
    ('filetype:pdf "prior authorization form" site:bcbs.com',           "insurers/bcbs"),

    # Specialty therapy
    ('filetype:pdf "prior authorization" "GLP-1" form',                 "specialty/glp1"),
    ('filetype:pdf "prior authorization" "biologics" fillable',         "specialty/biologics"),
    ('filetype:pdf "prior authorization" "oncology" request form',      "specialty/oncology"),
    ('filetype:pdf "prior authorization" "rheumatology" fillable',      "specialty/rheumatology"),

    # Adobe LiveCycle fillable forms
    ('filetype:pdf "prior authorization" "Adobe LiveCycle"',            "livecycle"),
]

# ── Settings ──────────────────────────────────────────────────────────────────
RESULTS_PER_QUERY = 20      # Google results to fetch per query
SEARCH_PAUSE      = 5.0     # seconds between queries (avoids rate limiting)
DOWNLOAD_PAUSE    = 1.5     # seconds between PDF downloads
MIN_PDF_SIZE      = 20_000  # bytes — ignore tiny stub/redirect PDFs

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
})

seen_urls   = set()
seen_hashes = set()


def sanitize(name: str) -> str:
    name = unquote(name)
    name = re.sub(r'[\\/*?:"<>|]+', "_", name)
    return re.sub(r"_+", "_", name.strip())[:120]


def download(url: str, folder: Path) -> bool:
    """Fetch URL, save if it's a valid PDF. Returns True on success."""
    if url in seen_urls:
        return False
    seen_urls.add(url)

    try:
        r = SESSION.get(url, timeout=30, stream=True, allow_redirects=True)
        r.raise_for_status()
        data = b"".join(r.iter_content(8192))
    except Exception as e:
        log.debug("  SKIP  %s  (%s)", url[:80], type(e).__name__)
        return False

    if not data.startswith(b"%PDF"):
        log.debug("  SKIP  not a PDF: %s", url[:80])
        return False

    if len(data) < MIN_PDF_SIZE:
        log.debug("  SKIP  too small (%d bytes): %s", len(data), url[:80])
        return False

    h = hashlib.md5(data).hexdigest()
    if h in seen_hashes:
        log.debug("  SKIP  duplicate content: %s", url[:80])
        return False
    seen_hashes.add(h)

    folder.mkdir(parents=True, exist_ok=True)
    parsed  = urlparse(url)
    domain  = parsed.netloc.replace("www.", "").split(".")[0]
    base    = os.path.basename(parsed.path) or "form.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    fname   = sanitize(f"{domain}_{base}")
    dest    = folder / fname
    if dest.exists():
        dest = folder / f"{dest.stem}_{h[:6]}.pdf"

    dest.write_bytes(data)
    log.info("  SAVED  %s  (%d KB)", dest.name, len(data) // 1024)
    return True


def run():
    total_saved = 0
    print(f"\nOutput folder: {OUTPUT_DIR}\n")

    for query, subfolder in tqdm(QUERIES, desc="Queries", unit="q"):
        dest = OUTPUT_DIR / subfolder
        print(f"\n[{subfolder}] {query}")

        try:
            urls = list(search(query, num_results=RESULTS_PER_QUERY, sleep_interval=2))
        except Exception as e:
            log.warning("  Search failed: %s", e)
            time.sleep(SEARCH_PAUSE)
            continue

        pdf_urls = [u for u in urls if u.lower().endswith(".pdf") or "pdf" in u.lower()]
        log.info("  Found %d results (%d PDF-looking)", len(urls), len(pdf_urls))

        saved = 0
        for url in pdf_urls:
            if download(url, dest):
                saved += 1
            time.sleep(DOWNLOAD_PAUSE)

        total_saved += saved
        log.info("  Saved %d from this query", saved)
        time.sleep(SEARCH_PAUSE)

    print(f"\n{'='*55}")
    print(f"Done. {total_saved} PDFs saved to {OUTPUT_DIR}")
    print(f"Unique URLs tried: {len(seen_urls)}")


if __name__ == "__main__":
    run()
