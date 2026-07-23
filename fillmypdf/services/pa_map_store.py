"""
pa_map_store.py
===============
Read-only accessor over the offline-built pa_forms.db.

The DB has three tables (written by pa_schema_extractor.py + pa_vision_mapper.py):

  canonical_fields  (name, entity, critical)
  forms             (form_id, file, payer, n_fields)
  form_fields       (form_id, raw_name, field_type, canonical_field, confidence,
                     [source])   -- source added by vision mapper

The primary lookup key for runtime fill is the form's ``field_signature``
(an MD5 of sorted "field_name:type" pairs, produced by pa_profiler.py).  When
a new PDF matches a stored signature it reuses the pre-built map instantly.
A file-path fallback is also supported for exact-match scenarios.

Thread-safety: each call opens + closes its own connection (SQLite allows this
for read-only workloads without issue).  The module-level singleton
``_default_store`` lazily opens the configured DB on first use.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from pypdf import PdfReader

from ..config import settings


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FieldMap:
    """Mapping for one raw PDF field -> canonical path."""
    raw_name: str
    field_type: str             # /Tx, /Btn, /Ch, /Sig …
    canonical_field: str        # dotted path, e.g. "patient.dob", or "UNMAPPED"
    confidence: str             # high | medium | low | none
    source: str = "name"        # name | vision | manual


@dataclass
class FormMap:
    """All field mappings for one form template."""
    form_id: Optional[int]
    file: Optional[str]
    payer: Optional[str]
    field_signature: Optional[str]
    fields: List[FieldMap]

    @property
    def mapped(self) -> List[FieldMap]:
        return [f for f in self.fields if f.canonical_field != "UNMAPPED"]

    @property
    def unmapped(self) -> List[FieldMap]:
        return [f for f in self.fields if f.canonical_field == "UNMAPPED"]

    def by_raw_name(self) -> Dict[str, FieldMap]:
        return {f.raw_name: f for f in self.fields}


# ---------------------------------------------------------------------------
# Field-signature calculation (mirrors pa_profiler.py)
# ---------------------------------------------------------------------------

def compute_field_signature(pdf_path) -> Optional[str]:
    """
    Compute the field_signature for a PDF the same way pa_profiler.py does:
    MD5 of sorted "name:type" pairs for all AcroForm fields.

    Accepts a file path (str | Path) **or** a BytesIO / bytes object so callers
    can compute the signature directly from in-memory PDF bytes without writing
    a temporary file.

    Returns None if the PDF has no AcroForm fields or on any error.
    """
    import io as _io
    try:
        if isinstance(pdf_path, (bytes, bytearray)):
            source = _io.BytesIO(pdf_path)
        elif isinstance(pdf_path, _io.IOBase):
            # BytesIO or other file-like — pass directly; PdfReader accepts it
            source = pdf_path
        else:
            source = str(pdf_path)
        reader = PdfReader(source, strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        fields = reader.get_fields() or {}
        if not fields:
            return None
        parts = sorted(f"{name}:{f.get('/FT', '')}" for name, f in fields.items())
        sig = "FORM:" + ";".join(parts)
        return hashlib.md5(sig.encode()).hexdigest()[:12]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class PAMapStore:
    """
    Read-only wrapper over pa_forms.db.

    Args:
        db_path: Path to pa_forms.db.  Defaults to settings.STORAGE_DIR / "pa_forms.db".
    """

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        if db_path is None:
            db_path = getattr(settings, "PA_FORMS_DB", None) or (
                settings.STORAGE_DIR / "pa_forms.db"
            )
        self._db = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        if not self._db.exists():
            raise FileNotFoundError(
                f"pa_forms.db not found at {self._db}. "
                "Run pa_schema_extractor.py first to build the map."
            )
        return sqlite3.connect(str(self._db))

    def _has_source_column(self, conn: sqlite3.Connection) -> bool:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(form_fields)")]
        return "source" in cols

    def _load_fields(self, conn: sqlite3.Connection, form_id: int) -> List[FieldMap]:
        has_src = self._has_source_column(conn)
        if has_src:
            rows = conn.execute(
                "SELECT raw_name, field_type, canonical_field, confidence, source "
                "FROM form_fields WHERE form_id = ?", (form_id,)
            ).fetchall()
            return [FieldMap(*r) for r in rows]
        else:
            rows = conn.execute(
                "SELECT raw_name, field_type, canonical_field, confidence "
                "FROM form_fields WHERE form_id = ?", (form_id,)
            ).fetchall()
            return [FieldMap(raw_name=r[0], field_type=r[1],
                             canonical_field=r[2], confidence=r[3]) for r in rows]

    def get_by_signature(self, signature: str) -> Optional[FormMap]:
        """
        Look up a form map by field_signature.

        Two strategies in order:
        1. ``form_signatures`` table (if it exists — written by the vision mapper).
        2. Rebuild signatures on-the-fly from ``form_fields`` data so that renamed
           or copied PDFs are still matched by their field fingerprint.
        """
        try:
            conn = self._connect()
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}

            row = None
            if "form_signatures" in tables:
                row = conn.execute(
                    "SELECT f.form_id, f.file, f.payer FROM forms f "
                    "JOIN form_signatures fs ON fs.form_id = f.form_id "
                    "WHERE fs.signature = ? ORDER BY f.form_id DESC LIMIT 1",
                    (signature,)
                ).fetchone()

            if row is None:
                # Rebuild signatures from stored field data (raw_name + field_type
                # per form) and find the first match.  This is a linear scan but
                # only runs when the faster path misses, and PDFs in the corpus are
                # typically ≤300.
                all_forms = conn.execute(
                    "SELECT form_id, file, payer FROM forms"
                ).fetchall()
                for fid, ffile, fpayer in all_forms:
                    field_rows = conn.execute(
                        "SELECT raw_name, field_type FROM form_fields WHERE form_id = ?",
                        (fid,)
                    ).fetchall()
                    parts = sorted(f"{r[0]}:{r[1]}" for r in field_rows)
                    computed = "FORM:" + ";".join(parts)
                    computed_sig = hashlib.md5(computed.encode()).hexdigest()[:12]
                    if computed_sig == signature:
                        row = (fid, ffile, fpayer)
                        break

            if row is None:
                conn.close()
                return None

            form_id, file_, payer = row
            fields = self._load_fields(conn, form_id)
            conn.close()
            return FormMap(
                form_id=form_id, file=file_, payer=payer,
                field_signature=signature, fields=fields,
            )
        except FileNotFoundError:
            raise
        except Exception:
            return None

    def get_by_file(self, file_path: str | Path) -> Optional[FormMap]:
        """Look up a form map by the file path stored in the forms table."""
        try:
            conn = self._connect()
            fp = str(file_path)
            row = conn.execute(
                "SELECT form_id, file, payer FROM forms WHERE file = ? LIMIT 1",
                (fp,)
            ).fetchone()
            if not row:
                # Try basename match
                row = conn.execute(
                    "SELECT form_id, file, payer FROM forms "
                    "WHERE file LIKE ? LIMIT 1",
                    (f"%{Path(fp).name}",)
                ).fetchone()
            if not row:
                conn.close()
                return None
            form_id, file_, payer = row
            fields = self._load_fields(conn, form_id)
            conn.close()
            return FormMap(
                form_id=form_id, file=file_, payer=payer,
                field_signature=None, fields=fields,
            )
        except FileNotFoundError:
            raise
        except Exception:
            return None

    def get_by_pdf_path(self, pdf_path: str | Path) -> Optional[FormMap]:
        """
        Best-effort lookup: compute the field_signature of the given PDF
        and try by signature, falling back to file-path match.
        """
        sig = compute_field_signature(pdf_path)
        result = None
        if sig:
            result = self.get_by_signature(sig)
        if result is None:
            result = self.get_by_file(pdf_path)
        return result

    def list_forms(self, limit: int = 100) -> List[dict]:
        """Return a summary list of all forms in the DB."""
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT f.form_id, f.file, f.payer, f.n_fields, "
                "COUNT(CASE WHEN ff.canonical_field != 'UNMAPPED' THEN 1 END) AS mapped, "
                "COUNT(CASE WHEN ff.confidence IN ('high') THEN 1 END) AS high_conf "
                "FROM forms f LEFT JOIN form_fields ff ON ff.form_id = f.form_id "
                "GROUP BY f.form_id ORDER BY f.form_id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            conn.close()
            return [
                {"form_id": r[0], "file": r[1], "payer": r[2],
                 "n_fields": r[3], "mapped": r[4], "high_conf": r[5]}
                for r in rows
            ]
        except FileNotFoundError:
            raise
        except Exception:
            return []

    def stats(self) -> dict:
        """Return aggregate stats about the DB."""
        try:
            conn = self._connect()
            n_forms = conn.execute("SELECT COUNT(*) FROM forms").fetchone()[0]
            n_fields = conn.execute("SELECT COUNT(*) FROM form_fields").fetchone()[0]
            n_mapped = conn.execute(
                "SELECT COUNT(*) FROM form_fields WHERE canonical_field != 'UNMAPPED'"
            ).fetchone()[0]
            n_critical_mapped = conn.execute(
                "SELECT COUNT(*) FROM form_fields ff "
                "JOIN canonical_fields cf ON cf.name = ff.canonical_field "
                "WHERE cf.critical = 1 AND ff.canonical_field != 'UNMAPPED'"
            ).fetchone()[0]
            conn.close()
            return {
                "forms": n_forms,
                "total_fields": n_fields,
                "mapped": n_mapped,
                "unmapped": n_fields - n_mapped,
                "map_rate": round(n_mapped / n_fields, 3) if n_fields else 0,
                "critical_fields_mapped": n_critical_mapped,
            }
        except FileNotFoundError:
            raise
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Module-level lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[PAMapStore] = None


def get_default_store() -> PAMapStore:
    global _default_store
    if _default_store is None:
        _default_store = PAMapStore()
    return _default_store
