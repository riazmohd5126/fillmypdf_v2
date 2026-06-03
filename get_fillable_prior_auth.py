#!/usr/bin/env python3
"""
Fillable Prior Authorization Form Downloader
Finds and downloads fillable (AcroForm) prior auth PDFs for autofill testing.

Strategy:
  1. Scrapes Bing public search results with filetype:pdf queries (no API key)
  2. Downloads from a curated list of known insurer direct URLs
  3. Validates each PDF has actual fillable form fields (AcroForm)
  4. Rejects scanned images, stub files, and non-fillable PDFs

Requirements:
    pip install requests beautifulsoup4 lxml tqdm pypdf

Usage:
    python get_fillable_prior_auth.py
    python get_fillable_prior_auth.py --output ~/Desktop/pa_forms --verbose
"""

import os
import re
import time
import hashlib
import logging
import argparse
import random
from pathlib import Path
from urllib.parse import urlparse, unquote, urljoin

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

try:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False
    print("WARNING: pypdf not installed — fillable check disabled. Run: pip install pypdf")

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Bing search queries  (filetype:pdf works in Bing's public HTML interface)
# ─────────────────────────────────────────────────────────────────────────────

BING_QUERIES: list[tuple[str, str]] = [
    # (query, output_folder)
    ('filetype:pdf "prior authorization form" fillable',                  "generic"),
    ('filetype:pdf "prior authorization request" fillable',               "generic"),
    ('filetype:pdf "prior authorization" "patient name" "prescriber"',    "generic"),
    ('filetype:pdf "prior authorization" "fax" "date of birth"',          "generic"),
    ('filetype:pdf "prior authorization" "procedure code" OR "CPT"',      "procedure"),
    ('filetype:pdf "prior authorization" "drug name" OR "medication"',    "medication"),
    ('filetype:pdf "prior authorization" "GLP-1" OR "Ozempic"',           "specialty/glp1"),
    ('filetype:pdf "prior authorization" "biologics" fillable',           "specialty/biologics"),
    ('filetype:pdf "prior authorization" "oncology"',                     "specialty/oncology"),
    ('filetype:pdf "prior authorization" "rheumatology"',                 "specialty/rheumatology"),
    ('filetype:pdf "prior authorization" site:uhc.com',                   "insurers/uhc"),
    ('filetype:pdf "prior authorization" site:cigna.com',                 "insurers/cigna"),
    ('filetype:pdf "prior authorization" site:aetna.com',                 "insurers/aetna"),
    ('filetype:pdf "prior authorization" site:anthem.com',                "insurers/anthem"),
    ('filetype:pdf "prior authorization" site:humana.com',                "insurers/humana"),
    ('filetype:pdf "prior authorization" site:optumrx.com',               "insurers/optum"),
    ('filetype:pdf "prior authorization" site:caremark.com',              "insurers/caremark"),
    ('filetype:pdf "prior authorization" site:express-scripts.com',       "insurers/evernorth"),
    ('filetype:pdf "prior authorization" "Adobe LiveCycle"',              "livecycle"),
    ('filetype:pdf "prior authorization" "step therapy"',                 "step_therapy"),
]

# ─────────────────────────────────────────────────────────────────────────────
# Curated direct URLs — known publicly posted prior auth forms
# ─────────────────────────────────────────────────────────────────────────────

DIRECT_URLS: list[tuple[str, str]] = [
    # CMS / Medicare
    ("https://www.cms.gov/Medicare/CMS-Forms/CMS-Forms/downloads/cms10148.pdf",           "cms"),
    ("https://www.cms.gov/Medicare/Prescription-Drug-coverage/PrescriptionDrugCovGenIn/downloads/model-coverage-determination-request.pdf", "cms"),

    # UnitedHealthcare
    ("https://www.uhcprovider.com/content/dam/provider/docs/public/prior-auth/pa-request-form.pdf", "insurers/uhc"),
    ("https://www.uhcprovider.com/content/dam/provider/docs/public/prior-auth/comm-prior-auth-request-form.pdf", "insurers/uhc"),

    # Aetna
    ("https://www.aetna.com/content/www/aetna/en/individuals-families/member-rights-resources/find-a-form/forms.html", "insurers/aetna"),

    # Cigna
    ("https://www.cigna.com/static/www-cigna-com/docs/health-care-providers/pharmacy-prior-authorization-request-form.pdf", "insurers/cigna"),

    # Humana
    ("https://www.humana.com/content/dam/humana/pdf/providers/prior-authorization-request-form.pdf", "insurers/humana"),

    # Anthem
    ("https://www.anthem.com/dam/medpolicies/abc/active/pa_form.pdf", "insurers/anthem"),

    # CVS Caremark
    ("https://www.caremark.com/portal/asset/PA_Request_Form.pdf", "insurers/caremark"),

    # Express Scripts / Evernorth
    ("https://www.express-scripts.com/art/static/pdf/PA_request_form.pdf", "insurers/evernorth"),

    # OptumRx
    ("https://www.optumrx.com/content/dam/optum3/optumrx/pdf/prior-auth-request-form.pdf", "insurers/optum"),

    # Molina
    ("https://www.molinahealthcare.com/providers/common/PDF/pa-request-form.pdf", "insurers/molina"),

    # WellCare
    ("https://www.wellcare.com/~/media/Documents/National/Providers/PA-Request-Form.pdf", "insurers/wellcare"),
]

# ─────────────────────────────────────────────────────────────────────────────
# HTTP session with rotating user agents
# ─────────────────────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
]

SESSION = requests.Session()

_seen_hashes: set[str] = set()
_seen_urls:   set[str] = set()


def get_headers(for_pdf: bool = False) -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/pdf,*/*" if for_pdf else "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Bing HTML scraper
# ─────────────────────────────────────────────────────────────────────────────

def bing_search(query: str, max_results: int = 30) -> list[str]:
    """
    Scrape Bing public HTML search results.
    Bing honors filetype:pdf in the query string.
    Returns a list of PDF URLs extracted from results.
    """
    urls: list[str] = []
    first = 1

    while len(urls) < max_results:
        params = {
            "q":     query,
            "first": first,
            "count": 10,
        }
        try:
            resp = SESSION.get(
                "https://www.bing.com/search",
                params=params,
                headers=get_headers(for_pdf=False),
                timeout=20,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            log.warning("Bing fetch failed: %s", exc)
            break

        soup = BeautifulSoup(resp.text, "lxml")

        # Bing result links are in <li class="b_algo"> → <h2> → <a href>
        found_this_page = 0
        for a in soup.select("li.b_algo h2 a[href]"):
            href = a.get("href", "")
            if href.startswith("http") and ".pdf" in href.lower():
                urls.append(href)
                found_this_page += 1

        # Also grab any direct .pdf links anywhere on the result page
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and href.lower().endswith(".pdf"):
                if href not in urls:
                    urls.append(href)
                    found_this_page += 1

        log.debug("  Bing page first=%d → %d PDF URLs", first, found_this_page)

        if found_this_page == 0:
            break  # no more results

        first += 10
        time.sleep(random.uniform(2.5, 4.5))  # polite delay between pages

    log.info("  Bing '%s'  → %d PDF URLs", query[:65], len(urls))
    return list(dict.fromkeys(urls))  # deduplicate while preserving order


# ─────────────────────────────────────────────────────────────────────────────
# PDF validation
# ─────────────────────────────────────────────────────────────────────────────

MIN_SIZE_BYTES = 20_000  # 20 KB — stubs and redirect PDFs are always smaller


def is_fillable_pdf(data: bytes) -> bool:
    """Return True if the PDF bytes contain AcroForm fields."""
    if not HAS_PYPDF:
        return True  # can't check — let it through
    try:
        import io
        reader = PdfReader(io.BytesIO(data))
        fields = reader.get_fields()
        return bool(fields)
    except Exception:
        return False


def pdf_field_count(data: bytes) -> int:
    """Return number of form fields, 0 if none or unreadable."""
    if not HAS_PYPDF:
        return -1
    try:
        import io
        reader = PdfReader(io.BytesIO(data))
        fields = reader.get_fields() or {}
        return len(fields)
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Download + save
# ─────────────────────────────────────────────────────────────────────────────

def sanitize(name: str, maxlen: int = 100) -> str:
    name = unquote(name)
    name = re.sub(r'[\\/*?:"<>|\']+', "_", name)
    name = re.sub(r"\s+", "_", name.strip())
    return re.sub(r"_+", "_", name)[:maxlen] or "form"


def url_to_filename(url: str, folder: str) -> str:
    parsed = urlparse(url)
    base = os.path.basename(parsed.path) or "form"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    domain = parsed.netloc.replace("www.", "").split(".")[0]
    return sanitize(f"{domain}_{base}")


def download_and_validate(url: str, dest_dir: Path, timeout: int = 30) -> tuple[bool, str]:
    """
    Download URL, validate it's a real fillable prior auth PDF.
    Returns (saved: bool, reason: str).
    """
    url = url.split("#")[0].strip()
    if url in _seen_urls:
        return False, "duplicate URL"
    _seen_urls.add(url)

    # Fetch
    try:
        resp = SESSION.get(
            url, timeout=timeout, stream=True,
            allow_redirects=True, headers=get_headers(for_pdf=True),
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        return False, f"fetch error: {type(exc).__name__}"

    # Read + magic byte check
    head = b""
    for chunk in resp.iter_content(2048):
        head = chunk
        break

    if not head.startswith(b"%PDF"):
        resp.close()
        return False, "not a PDF"

    try:
        data = head + b"".join(resp.iter_content(8192))
    except Exception:
        return False, "read error"

    # Size check
    if len(data) < MIN_SIZE_BYTES:
        return False, f"too small ({len(data)//1024} KB) — likely a stub/redirect"

    # Duplicate content check
    file_hash = hashlib.md5(data).hexdigest()
    if file_hash in _seen_hashes:
        return False, "duplicate content"
    _seen_hashes.add(file_hash)

    # Fillable check
    fields = pdf_field_count(data)
    if HAS_PYPDF and fields == 0:
        return False, "not fillable (no AcroForm fields — scanned image or flat PDF)"

    # Save
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / url_to_filename(url, str(dest_dir))
    if dest_path.exists():
        dest_path = dest_dir / f"{dest_path.stem}_{file_hash[:6]}.pdf"

    dest_path.write_bytes(data)
    field_info = f"{fields} fields" if fields > 0 else "fields unknown"
    log.info("  SAVED  %-55s  %d KB  [%s]", dest_path.name, len(data)//1024, field_info)
    return True, f"OK ({field_info})"


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def run(
    base_dir: Path,
    max_per_query: int,
    search_delay: float,
    download_delay: float,
) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    total_saved = 0
    total_tried = 0
    reject_reasons: dict[str, int] = {}

    # ── Phase 1: Bing HTML scraping ───────────────────────────────────────────
    print(f"\n{'='*60}")
    print("PHASE 1 — Bing search (filetype:pdf prior auth queries)")
    print(f"{'='*60}\n")

    for query, folder in tqdm(BING_QUERIES, desc="Bing queries", unit="q"):
        dest_dir = base_dir / folder
        urls = bing_search(query, max_results=max_per_query)

        for url in urls:
            total_tried += 1
            saved, reason = download_and_validate(url, dest_dir)
            if saved:
                total_saved += 1
            else:
                reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
                log.debug("  SKIP  %s  (%s)", url[:80], reason)
            time.sleep(download_delay)

        time.sleep(search_delay)

    # ── Phase 2: Direct known URLs ────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("PHASE 2 — Direct known insurer URLs")
    print(f"{'='*60}\n")

    for url, folder in tqdm(DIRECT_URLS, desc="Direct URLs", unit="url"):
        dest_dir = base_dir / folder
        total_tried += 1
        saved, reason = download_and_validate(url, dest_dir)
        if saved:
            total_saved += 1
        else:
            reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
            log.debug("  SKIP  %s  (%s)", url[:80], reason)
        time.sleep(download_delay)

    # ── Summary ───────────────────────────────────────────────────────────────
    log_file = base_dir / "download_log.txt"
    with open(log_file, "w") as f:
        f.write("Fillable Prior Auth PDF Download Log\n")
        f.write(f"Date   : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Folder : {base_dir.resolve()}\n\n")
        f.write(f"Tried  : {total_tried}\n")
        f.write(f"Saved  : {total_saved}\n\n")
        f.write("Rejection reasons:\n")
        for reason, count in sorted(reject_reasons.items(), key=lambda x: -x[1]):
            f.write(f"  {count:4d}x  {reason}\n")

    print(f"\n{'='*60}")
    print(f"Done.  {total_saved} fillable PDFs saved → {base_dir.resolve()}")
    print(f"       (tried {total_tried}, rejected {total_tried - total_saved})\n")
    if reject_reasons:
        print("Rejection breakdown:")
        for reason, count in sorted(reject_reasons.items(), key=lambda x: -x[1]):
            print(f"  {count:4d}x  {reason}")
    print(f"\nLog: {log_file}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download fillable prior auth PDFs via Bing search + direct URLs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--output", "-o",
        default=str(Path.home() / "prior_auth_forms"),
        help="Output folder (default: ~/prior_auth_forms)")
    parser.add_argument("--max-per-query", "-n", type=int, default=30,
        help="Max PDF URLs to attempt per Bing query (default: 30)")
    parser.add_argument("--search-delay", type=float, default=5.0,
        help="Seconds between Bing queries (default: 5.0)")
    parser.add_argument("--download-delay", type=float, default=1.5,
        help="Seconds between PDF downloads (default: 1.5)")
    parser.add_argument("--verbose", "-v", action="store_true",
        help="Show debug logs including skip reasons")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if not HAS_PYPDF:
        log.warning("pypdf not found — fillable check disabled. pip install pypdf")

    run(
        base_dir=Path(args.output),
        max_per_query=args.max_per_query,
        search_delay=args.search_delay,
        download_delay=args.download_delay,
    )


if __name__ == "__main__":
    main()
