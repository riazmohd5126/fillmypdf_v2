#!/usr/bin/env python3
"""
Prior Auth PDF Downloader — Browser Edition
Opens a real Chrome browser, runs each Google dork, extracts PDF links,
downloads them. Google sees it as a normal user search.

Install:
    pip install selenium webdriver-manager requests tqdm

Run:
    python prior_auth_downloader.py

Chrome must be installed on your laptop (it almost certainly is).
"""

import os, re, time, hashlib, logging
from pathlib import Path
from urllib.parse import urlparse, unquote, quote_plus

import requests
from tqdm import tqdm
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ── Output folder ─────────────────────────────────────────────────────────────
OUTPUT_DIR = Path.home() / "prior_auth_forms"

# ── Google dork queries ───────────────────────────────────────────────────────
QUERIES = [
    # ── Original dorks ────────────────────────────────────────────────────────
    ('filetype:pdf "prior authorization form" fillable',                 "generic"),
    ('filetype:pdf "prior authorization request" form',                  "generic"),
    ('filetype:pdf "prior authorization" "specialty pharmacy" fillable', "generic"),
    ('filetype:pdf "step therapy" "prior authorization form"',           "generic"),
    ('filetype:pdf "prior authorization form" site:uhc.com',             "insurers/uhc"),
    ('filetype:pdf "prior authorization form" site:cigna.com',           "insurers/cigna"),
    ('filetype:pdf "prior authorization form" site:aetna.com',           "insurers/aetna"),
    ('filetype:pdf "prior authorization form" site:anthem.com',          "insurers/anthem"),
    ('filetype:pdf "prior authorization form" site:bcbs.com',            "insurers/bcbs"),
    ('filetype:pdf "prior authorization" "GLP-1" form',                  "specialty/glp1"),
    ('filetype:pdf "prior authorization" "biologics" fillable',          "specialty/biologics"),
    ('filetype:pdf "prior authorization" "oncology" request form',       "specialty/oncology"),
    ('filetype:pdf "prior authorization" "rheumatology" fillable',       "specialty/rheumatology"),
    ('filetype:pdf "prior authorization" "Adobe LiveCycle"',             "livecycle"),

    # ── Universal PA Form Dorks ───────────────────────────────────────────────
    ('filetype:pdf "prior authorization request form"',                  "universal"),
    ('filetype:pdf "prior authorization form"',                          "universal"),
    ('filetype:pdf "authorization request form"',                        "universal"),
    ('filetype:pdf "coverage determination request"',                    "universal"),
    ('filetype:pdf "medical exception request"',                         "universal"),
    ('filetype:pdf "predetermination request form"',                     "universal"),
    ('filetype:pdf "request for coverage"',                              "universal"),
    ('filetype:pdf "specialty medication request form"',                 "universal"),

    # ── Field-Based Dorks (form field labels) ─────────────────────────────────
    ('filetype:pdf "patient name" "prescriber name"',                    "fields"),
    ('filetype:pdf "member id" "prescriber"',                            "fields"),
    ('filetype:pdf "date of birth" "prescriber"',                        "fields"),
    ('filetype:pdf "requesting provider"',                               "fields"),
    ('filetype:pdf "provider information"',                              "fields"),
    ('filetype:pdf "patient information" "provider information"',        "fields"),
    ('filetype:pdf "office contact"',                                    "fields"),
    ('filetype:pdf "diagnosis code"',                                    "fields"),
    ('filetype:pdf "ICD-10"',                                            "fields"),
    ('filetype:pdf "NPI Number"',                                        "fields"),

    # ── Fax-Based Dorks ───────────────────────────────────────────────────────
    ('filetype:pdf "fax completed form"',                                "fax"),
    ('filetype:pdf "fax this request"',                                  "fax"),
    ('filetype:pdf "fax completed prior authorization"',                 "fax"),
    ('filetype:pdf "fax number"',                                        "fax"),
    ('filetype:pdf "urgent review"',                                     "fax"),
    ('filetype:pdf "expedited review"',                                  "fax"),

    # ── Clinical Criteria Dorks ───────────────────────────────────────────────
    ('filetype:pdf "clinical information"',                              "clinical"),
    ('filetype:pdf "medical necessity"',                                 "clinical"),
    ('filetype:pdf "supporting documentation"',                          "clinical"),
    ('filetype:pdf "chart notes"',                                       "clinical"),
    ('filetype:pdf "clinical rationale"',                                "clinical"),
    ('filetype:pdf "treatment history"',                                 "clinical"),

    # ── Drug-Specific Dorks ───────────────────────────────────────────────────
    ('filetype:pdf Ozempic "prior authorization"',                       "drugs/ozempic"),
    ('filetype:pdf Wegovy "prior authorization"',                        "drugs/wegovy"),
    ('filetype:pdf Mounjaro "prior authorization"',                      "drugs/mounjaro"),
    ('filetype:pdf Zepbound "prior authorization"',                      "drugs/zepbound"),
    ('filetype:pdf Skyrizi "prior authorization"',                       "drugs/skyrizi"),
    ('filetype:pdf Dupixent "prior authorization"',                      "drugs/dupixent"),
    ('filetype:pdf Humira "prior authorization"',                        "drugs/humira"),
    ('filetype:pdf Enbrel "prior authorization"',                        "drugs/enbrel"),
    ('filetype:pdf Rinvoq "prior authorization"',                        "drugs/rinvoq"),
    ('filetype:pdf Xeljanz "prior authorization"',                       "drugs/xeljanz"),
    ('filetype:pdf Cosentyx "prior authorization"',                      "drugs/cosentyx"),
    ('filetype:pdf Stelara "prior authorization"',                       "drugs/stelara"),

    # ── Diagnosis-Specific Dorks ──────────────────────────────────────────────
    ('filetype:pdf "rheumatoid arthritis" "prior authorization"',        "diagnosis/rheumatoid_arthritis"),
    ('filetype:pdf "psoriasis" "prior authorization"',                   "diagnosis/psoriasis"),
    ('filetype:pdf "crohn\'s disease" "prior authorization"',            "diagnosis/crohns"),
    ('filetype:pdf "ulcerative colitis" "prior authorization"',          "diagnosis/ulcerative_colitis"),
    ('filetype:pdf "migraine" "prior authorization"',                    "diagnosis/migraine"),
    ('filetype:pdf "ADHD" "prior authorization"',                        "diagnosis/adhd"),

    # ── PBM-Specific Dorks ────────────────────────────────────────────────────
    ('site:caremark.com filetype:pdf "prior authorization"',             "pbm/caremark"),
    ('site:caremark.com filetype:pdf "request form"',                    "pbm/caremark"),
    ('site:caremark.com filetype:pdf "specialty medication"',            "pbm/caremark"),
    ('site:optumrx.com filetype:pdf "prior authorization"',              "pbm/optumrx"),
    ('site:express-scripts.com filetype:pdf "prior authorization"',      "pbm/express_scripts"),
    ('site:primetherapeutics.com filetype:pdf "prior authorization"',    "pbm/prime_therapeutics"),
    ('site:medimpact.com filetype:pdf "prior authorization"',            "pbm/medimpact"),
    ('site:navitus.com filetype:pdf "prior authorization"',              "pbm/navitus"),
    ('site:maxor.com filetype:pdf "prior authorization"',                "pbm/maxor"),

    # ── Medicare Part D Dorks ─────────────────────────────────────────────────
    ('filetype:pdf "medicare part d" "coverage determination"',          "medicare_part_d"),
    ('filetype:pdf "redetermination request form"',                      "medicare_part_d"),
    ('filetype:pdf "part d prior authorization"',                        "medicare_part_d"),
    ('filetype:pdf "medicare prescription drug coverage"',               "medicare_part_d"),

    # ── Medicaid Dorks ────────────────────────────────────────────────────────
    ('filetype:pdf Medicaid "prior authorization"',                      "medicaid"),
    ('filetype:pdf Medicaid "PA request form"',                          "medicaid"),
    ('filetype:pdf "state medicaid" "prior authorization"',              "medicaid"),
    ('site:.gov filetype:pdf "prior authorization request"',             "medicaid/gov"),

    # ── Specialty Pharmacy Forms ──────────────────────────────────────────────
    ('filetype:pdf "specialty pharmacy" "patient information"',          "specialty_pharmacy"),
    ('filetype:pdf "specialty enrollment form"',                         "specialty_pharmacy"),
    ('filetype:pdf "prescription referral form"',                        "specialty_pharmacy"),
    ('filetype:pdf "therapy initiation form"',                           "specialty_pharmacy"),

    # ── Form Repository Dorks ─────────────────────────────────────────────────
    ('inurl:prior-authorization filetype:pdf',                           "repositories"),
    ('inurl:priorauth filetype:pdf',                                     "repositories"),
    ('inurl:forms filetype:pdf "prior authorization"',                   "repositories"),
    ('intitle:"Prior Authorization" filetype:pdf',                       "repositories"),
    ('intitle:"Authorization Request Form" filetype:pdf',                "repositories"),
    ('filetype:pdf site:sharepoint.com "prior authorization"',           "repositories/sharepoint"),
    ('filetype:pdf site:box.com "prior authorization"',                  "repositories/box"),
    ('filetype:pdf site:dropbox.com "prior authorization"',              "repositories/dropbox"),

    # ── State-Specific Medicaid ───────────────────────────────────────────────
    ('filetype:pdf Texas Medicaid "prior authorization"',                "states/texas"),
    ('filetype:pdf California Medicaid "prior authorization"',           "states/california"),
    ('filetype:pdf Florida Medicaid "prior authorization"',              "states/florida"),
    ('filetype:pdf "New York" Medicaid "prior authorization"',           "states/new_york"),
    ('filetype:pdf "Rhode Island" Medicaid "prior authorization"',       "states/rhode_island"),
]

RESULTS_PER_QUERY = 30   # Google results to scan per query (increase for more PDFs)
SEARCH_PAUSE      = 6.0  # seconds between queries
DOWNLOAD_PAUSE    = 1.5  # seconds between downloads
MIN_PDF_SIZE      = 20_000

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


def make_driver() -> webdriver.Chrome:
    """Launch Chrome. Browser window is visible so you can solve CAPTCHAs."""
    opts = Options()
    # opts.add_argument("--headless=new")   # uncomment to run silently
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    service = Service(ChromeDriverManager().install())
    driver  = webdriver.Chrome(service=service, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def extract_pdf_links(driver: webdriver.Chrome) -> list[str]:
    """Extract all PDF-looking hrefs from the current page."""
    anchors  = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    pdf_urls = []
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        if "/url?q=" in href:
            href = href.split("/url?q=")[1].split("&")[0]
        if "pdf" in href.lower() and href.startswith("http"):
            if href not in pdf_urls:
                pdf_urls.append(href)
    return pdf_urls


def google_search(driver: webdriver.Chrome, query: str, num: int = 30) -> list[str]:
    """Search Google with a dork query across pages 1 and 2, return all PDF hrefs."""
    all_pdf_urls = []

    for start in [0, num]:   # page 1, then page 2
        url = f"https://www.google.com/search?q={quote_plus(query)}&num={num}&start={start}"
        driver.get(url)
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div#search a"))
            )
        except Exception:
            pass
        time.sleep(2)

        links = extract_pdf_links(driver)
        all_pdf_urls.extend(l for l in links if l not in all_pdf_urls)

        # Stop if no results on this page
        if not links:
            break
        time.sleep(2)

    log.info("  [Google] '%s'  → %d PDF links", query[:65], len(all_pdf_urls))
    return all_pdf_urls


def download(url: str, folder: Path) -> bool:
    if url in seen_urls:
        return False
    seen_urls.add(url)

    try:
        r = SESSION.get(url, timeout=30, stream=True, allow_redirects=True)
        r.raise_for_status()
        data = b"".join(r.iter_content(8192))
    except Exception as e:
        log.debug("  fetch failed (%s): %s", type(e).__name__, url[:80])
        return False

    if not data.startswith(b"%PDF"):
        return False
    if len(data) < MIN_PDF_SIZE:
        log.debug("  too small (%d KB): %s", len(data) // 1024, url[:80])
        return False

    h = hashlib.md5(data).hexdigest()
    if h in seen_hashes:
        return False
    seen_hashes.add(h)

    folder.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "").split(".")[0]
    base   = os.path.basename(parsed.path) or "form.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    fname  = re.sub(r'[\\/*?:"<>|]+', "_", unquote(f"{domain}_{base}"))[:120]
    dest   = folder / fname
    if dest.exists():
        dest = folder / f"{dest.stem}_{h[:6]}.pdf"

    dest.write_bytes(data)
    log.info("  SAVED  %s  (%d KB)", dest.name, len(data) // 1024)
    return True


def run():
    print(f"\nOutput folder: {OUTPUT_DIR}")
    print("Opening Chrome — you will see a browser window appear.")
    print("If Google shows a CAPTCHA, solve it in that window,")
    print("then press Enter in this terminal to continue.\n")

    driver = make_driver()
    total  = 0

    try:
        for query, subfolder in tqdm(QUERIES, desc="Queries", unit="q"):
            print(f"\n→ {query}")
            dest = OUTPUT_DIR / subfolder

            try:
                pdf_urls = google_search(driver, query, num=RESULTS_PER_QUERY)
            except Exception as e:
                log.warning("  Search error: %s", e)
                time.sleep(SEARCH_PAUSE)
                continue

            # CAPTCHA check
            if not pdf_urls:
                src = driver.page_source.lower()
                if "captcha" in src or "unusual traffic" in src:
                    input("\n  CAPTCHA detected — solve it in the browser window, "
                          "then press Enter here to continue: ")
                    pdf_urls = google_search(driver, query, num=RESULTS_PER_QUERY)

            saved = 0
            for url in pdf_urls:
                if download(url, dest):
                    saved += 1
                time.sleep(DOWNLOAD_PAUSE)

            total += saved
            log.info("  %d PDF(s) saved for this query", saved)
            time.sleep(SEARCH_PAUSE)

    finally:
        driver.quit()

    print(f"\n{'='*55}")
    print(f"Done. {total} PDFs saved to {OUTPUT_DIR}")
    print(f"URLs tried: {len(seen_urls)}")


if __name__ == "__main__":
    run()
