"""
pa_normalize.py
===============
Type-aware normalization and validation for PA canonical fields.

The `type` string on each CanonicalField drives both:
  - normalize(value, type) -> canonical string suitable for writing into a PDF
  - validate(value, type) -> (ok: bool, reason: str)

Types handled:
  text, date, number, enum, checkbox, signature
  npi, dea, ndc, jcode, icd10, cpt, member_id, tax_id, ssn, phone, email, zip, list

All functions are pure (no I/O) and accept None gracefully.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _norm_date(value: str) -> Optional[str]:
    """Parse many date formats -> MM/DD/YYYY. Returns None on failure."""
    s = (value or "").strip()
    # Already MM/DD/YYYY
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", s)
    if m:
        mo, day, yr = m.groups()
        yr = ("20" + yr) if len(yr) == 2 else yr
        return f"{int(mo):02d}/{int(day):02d}/{yr}"
    # YYYY-MM-DD (ISO)
    m = re.match(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$", s)
    if m:
        yr, mo, day = m.groups()
        return f"{int(mo):02d}/{int(day):02d}/{yr}"
    # Try stdlib as last resort
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            d = datetime.strptime(s, fmt)
            return d.strftime("%m/%d/%Y")
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# NPI — 10-digit Luhn-variant check
# ---------------------------------------------------------------------------

def _npi_luhn(npi: str) -> bool:
    """NPPES Luhn check: prepend 80840, double every other from right, sum digits."""
    if not re.match(r"^\d{10}$", npi):
        return False
    s = "80840" + npi
    total = 0
    for i, ch in enumerate(reversed(s)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---------------------------------------------------------------------------
# DEA — 2-letter prefix + 7 digits, checksum on digits
# ---------------------------------------------------------------------------

def _dea_checksum(dea: str) -> bool:
    m = re.match(r"^[A-Za-z]{2}(\d{7})$", dea)
    if not m:
        return False
    digits = [int(c) for c in m.group(1)]
    odd = digits[0] + digits[2] + digits[4]
    even2 = (digits[1] + digits[3] + digits[5]) * 2
    check_digit = (odd + even2) % 10
    return check_digit == digits[6]


# ---------------------------------------------------------------------------
# NDC — normalize to 11-digit 5-4-2 with hyphens
# ---------------------------------------------------------------------------

def _norm_ndc(raw: str) -> Optional[str]:
    d = _digits(raw)
    if len(d) == 10:
        # Could be 4-4-2, 5-3-2, 5-4-1 — pad to 11 digits heuristically
        # Most common: 5-3-2 -> 5-4-2 (pad middle)
        d = d[:5] + "0" + d[5:]
    if len(d) != 11:
        return None
    return f"{d[:5]}-{d[5:9]}-{d[9:]}"


# ---------------------------------------------------------------------------
# ICD-10
# ---------------------------------------------------------------------------

_ICD10_RE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$", re.I)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize(value: Optional[str], field_type: str) -> Optional[str]:
    """
    Normalize a raw value to the canonical form for writing to a PDF.
    Returns None if the value is None/empty or cannot be normalized.
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None

    t = (field_type or "text").lower()

    if t == "date":
        return _norm_date(v)

    if t == "phone":
        d = _digits(v)
        if len(d) == 10:
            return f"({d[:3]}) {d[3:6]}-{d[6:]}"
        if len(d) == 11 and d[0] == "1":
            return f"({d[1:4]}) {d[4:7]}-{d[7:]}"
        return v  # pass through if unusual

    if t == "npi":
        d = _digits(v)
        return d if len(d) == 10 else v

    if t == "dea":
        clean = re.sub(r"[^A-Za-z0-9]", "", v).upper()
        return clean

    if t == "ndc":
        return _norm_ndc(v) or v

    if t in ("tax_id", "tin"):
        d = _digits(v)
        return f"{d[:2]}-{d[2:]}" if len(d) == 9 else v

    if t == "ssn":
        d = _digits(v)
        return f"{d[:3]}-{d[3:5]}-{d[5:]}" if len(d) == 9 else v

    if t == "zip":
        d = _digits(v)
        if len(d) == 9:
            return f"{d[:5]}-{d[5:]}"
        return d[:5] if len(d) >= 5 else v

    if t == "icd10":
        clean = re.sub(r"\s", "", v).upper()
        # Ensure dot is present: ABC1 -> A.BC1 (wrong), handle common no-dot form
        if "." not in clean and len(clean) >= 3:
            clean = clean[:3] + ("." + clean[3:] if len(clean) > 3 else "")
        return clean

    if t == "number":
        try:
            f = float(v.replace(",", ""))
            return str(int(f)) if f == int(f) else str(f)
        except ValueError:
            return v

    if t == "checkbox":
        low = v.lower()
        if low in ("1", "true", "yes", "x", "on", "checked"):
            return "Yes"
        if low in ("0", "false", "no", "", "off", "unchecked"):
            return "No"
        return v

    if t == "enum":
        return v.strip()

    # text, email, cpt, jcode, member_id, signature, list — return as-is
    return v


def validate(value: Optional[str], field_type: str) -> Tuple[bool, str]:
    """
    Validate a normalized value. Returns (ok, reason).
    A passing validation returns (True, "").
    """
    if value is None or not str(value).strip():
        return False, "empty"

    v = str(value).strip()
    t = (field_type or "text").lower()

    if t == "npi":
        d = _digits(v)
        if len(d) != 10:
            return False, f"NPI must be 10 digits, got {len(d)}"
        if not _npi_luhn(d):
            return False, "NPI fails Luhn check"
        return True, ""

    if t == "dea":
        clean = re.sub(r"[^A-Za-z0-9]", "", v).upper()
        if not re.match(r"^[A-Z]{2}\d{7}$", clean):
            return False, "DEA must be 2 letters + 7 digits"
        if not _dea_checksum(clean):
            return False, "DEA fails checksum"
        return True, ""

    if t == "ndc":
        norm = _norm_ndc(v)
        if norm is None:
            return False, f"NDC cannot be normalized to 11-digit from '{v}'"
        return True, ""

    if t == "icd10":
        clean = re.sub(r"\s", "", v).upper()
        if not _ICD10_RE.match(clean):
            return False, f"ICD-10 format invalid: '{clean}'"
        return True, ""

    if t == "date":
        if _norm_date(v) is None:
            return False, f"Cannot parse date: '{v}'"
        return True, ""

    if t == "phone":
        d = _digits(v)
        if len(d) not in (10, 11):
            return False, f"Phone must have 10-11 digits, got {len(d)}"
        return True, ""

    if t in ("tax_id", "tin"):
        d = _digits(v)
        if len(d) != 9:
            return False, f"Tax ID must be 9 digits, got {len(d)}"
        return True, ""

    if t == "ssn":
        d = _digits(v)
        if len(d) != 9:
            return False, f"SSN must be 9 digits, got {len(d)}"
        return True, ""

    if t == "zip":
        d = _digits(v)
        if len(d) not in (5, 9):
            return False, f"ZIP must be 5 or 9 digits, got {len(d)}"
        return True, ""

    if t == "member_id":
        if len(v) < 3:
            return False, "Member ID too short"
        return True, ""

    if t == "number":
        try:
            float(v.replace(",", ""))
            return True, ""
        except ValueError:
            return False, f"Not a number: '{v}'"

    if t == "email":
        if "@" not in v or "." not in v.split("@")[-1]:
            return False, f"Email format invalid: '{v}'"
        return True, ""

    # text, enum, checkbox, jcode, cpt, signature, list — non-empty is sufficient
    return True, ""


def normalize_and_validate(
    value: Optional[str], field_type: str
) -> Tuple[Optional[str], bool, str]:
    """Convenience: normalize then validate. Returns (normalized, ok, reason)."""
    normed = normalize(value, field_type)
    ok, reason = validate(normed, field_type)
    return normed, ok, reason
