"""
PDF Utility Routes — Merge & Split
====================================
  POST /api/v1/pdf/merge   — Merge 2-20 PDFs into one (multipart upload)
  POST /api/v1/pdf/split   — Split a PDF into individual pages or page ranges
  GET  /api/v1/pdf/download/{filename} — Download utility output
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pypdf import PdfReader, PdfWriter

from ...config import settings
from ..dependencies.auth import require_api_key


router = APIRouter(
    prefix="/pdf",
    tags=["pdf-utilities"],
    dependencies=[Depends(require_api_key)],
)

MAX_PDF_BYTES = 52_428_800   # 50 MiB per file
MAX_MERGE_FILES = 20


def _out_dir() -> Path:
    p = settings.OUTPUT_DIR / "pdf_utils"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Merge ──────────────────────────────────────────────────────────────────

@router.post("/merge", summary="Merge multiple PDFs into one")
async def merge_pdfs(
    files: List[UploadFile] = File(..., description="2–20 PDF files to merge in order"),
    output_name: Optional[str] = Form(None, description="Optional filename for the merged PDF (without extension)"),
):
    """
    Merge 2–20 PDF files into a single PDF, preserving page order.
    Files are merged in the order they are uploaded.
    """
    if len(files) < 2:
        raise HTTPException(400, "At least 2 PDF files are required.")
    if len(files) > MAX_MERGE_FILES:
        raise HTTPException(400, f"Maximum {MAX_MERGE_FILES} files per merge.")

    writer = PdfWriter()
    for i, f in enumerate(files):
        if not (f.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, f"File {i + 1} ('{f.filename}') is not a PDF.")
        raw = await f.read()
        if len(raw) > MAX_PDF_BYTES:
            raise HTTPException(400, f"File {i + 1} exceeds 50 MiB limit.")
        try:
            reader = PdfReader(io.BytesIO(raw))
            writer.append(reader)
        except Exception as exc:
            raise HTTPException(422, f"Could not read file {i + 1}: {exc}")

    uid = uuid.uuid4().hex[:12]
    safe_name = "".join(c for c in (output_name or "merged") if c.isalnum() or c in "-_")[:60] or "merged"
    out_filename = f"{safe_name}_{uid}.pdf"
    out_path = _out_dir() / out_filename

    with open(out_path, "wb") as fh:
        writer.write(fh)

    total_pages = len(writer.pages)
    return {
        "success": True,
        "filename": out_filename,
        "download_url": f"/api/v1/pdf/download/{out_filename}",
        "total_pages": total_pages,
        "files_merged": len(files),
        "message": f"Merged {len(files)} PDFs into {total_pages} pages.",
    }


# ── Split ──────────────────────────────────────────────────────────────────

@router.post("/split", summary="Split a PDF into pages or ranges")
async def split_pdf(
    file: UploadFile = File(..., description="PDF to split"),
    mode: str = Form(
        "pages",
        description=(
            "'pages' — one PDF per page; "
            "'range' — custom page ranges (requires page_ranges); "
            "'half' — split into two equal halves"
        ),
    ),
    page_ranges: Optional[str] = Form(
        None,
        description=(
            "For mode='range': comma-separated ranges, e.g. '1-3,4-7,8' "
            "(1-indexed, inclusive). Overlaps allowed."
        ),
    ),
):
    """
    Split a PDF into multiple files.

    **mode=pages** — produces one PDF per page (e.g. 10-page PDF → 10 files).
    **mode=range** — splits at custom boundaries (page_ranges required).
    **mode=half**  — splits at the midpoint into two files.

    Returns a list of download URLs, one per output file.
    """
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "File must be a PDF.")

    raw = await file.read()
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(400, "File exceeds 50 MiB limit.")

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(422, f"Could not read PDF: {exc}")

    n = len(reader.pages)
    if n == 0:
        raise HTTPException(422, "PDF has no pages.")

    uid = uuid.uuid4().hex[:12]
    base_name = Path(file.filename or "split").stem[:40]

    # Build list of (label, page_indices) tuples
    segments: list[tuple[str, list[int]]] = []

    if mode == "pages":
        for i in range(n):
            segments.append((f"page_{i + 1:03d}", [i]))

    elif mode == "half":
        mid = n // 2
        segments.append(("part1", list(range(0, mid))))
        segments.append(("part2", list(range(mid, n))))

    elif mode == "range":
        if not page_ranges:
            raise HTTPException(400, "page_ranges is required for mode='range'.")
        for part_str in page_ranges.split(","):
            part_str = part_str.strip()
            if not part_str:
                continue
            if "-" in part_str:
                parts = part_str.split("-", 1)
                try:
                    start = int(parts[0].strip()) - 1
                    end = int(parts[1].strip()) - 1
                except ValueError:
                    raise HTTPException(400, f"Invalid range '{part_str}'.")
                if start < 0 or end >= n or start > end:
                    raise HTTPException(400, f"Range '{part_str}' out of bounds (PDF has {n} pages).")
                label = f"pages_{start + 1}-{end + 1}"
                segments.append((label, list(range(start, end + 1))))
            else:
                try:
                    pg = int(part_str.strip()) - 1
                except ValueError:
                    raise HTTPException(400, f"Invalid page number '{part_str}'.")
                if pg < 0 or pg >= n:
                    raise HTTPException(400, f"Page {pg + 1} out of bounds (PDF has {n} pages).")
                segments.append((f"page_{pg + 1:03d}", [pg]))
    else:
        raise HTTPException(400, "mode must be 'pages', 'range', or 'half'.")

    if not segments:
        raise HTTPException(400, "No segments produced — check page_ranges.")

    results = []
    for label, indices in segments:
        w = PdfWriter()
        for idx in indices:
            w.add_page(reader.pages[idx])
        out_filename = f"{base_name}_{label}_{uid}.pdf"
        out_path = _out_dir() / out_filename
        with open(out_path, "wb") as fh:
            w.write(fh)
        results.append({
            "label": label,
            "pages": [i + 1 for i in indices],
            "page_count": len(indices),
            "filename": out_filename,
            "download_url": f"/api/v1/pdf/download/{out_filename}",
        })

    return {
        "success": True,
        "mode": mode,
        "source_pages": n,
        "output_files": len(results),
        "files": results,
    }


# ── Download ───────────────────────────────────────────────────────────────

@router.get("/download/{filename}", summary="Download a PDF utility output file")
async def download_util_output(filename: str):
    # Sanitise — prevent path traversal
    safe = Path(filename).name
    path = _out_dir() / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(404, f"File '{safe}' not found.")
    return FileResponse(path=str(path), media_type="application/pdf", filename=safe)
