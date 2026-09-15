"""
Office Convert Service — images and Word letters -> PDF pages
================================================================
Clinical docs arrive as phone photos, JPG scans, and DOCX letters of
medical necessity. This turns each into PDF pages that can then be merged
into a submission packet with the existing PDF Tools -> Merge tool.

  - Images (JPG/PNG/...): Pillow, in-process, no external dependency.
  - DOCX: LibreOffice headless (``soffice --convert-to pdf``) — the only
    approach on this stack that preserves letterhead, tables and embedded
    images the way Word would render them; a pure-Python text dump would
    lose that fidelity for a document staff are attaching as-is.

Each LibreOffice invocation gets its own throwaway user profile
(``-env:UserInstallation``) — soffice user profiles are not safe for
concurrent processes to share, and a server handling more than one
conversion at a time WILL corrupt/hang without this.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import List

from PIL import Image
from pypdf import PdfReader

try:
    # Registers .heic/.heif with Image.open() — iPhones default the camera
    # to this format, so "phone photos" means HEIC far more often than JPG.
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - degrades to a clear per-file error
    pass

_SOFFICE_TIMEOUT_SECONDS = 60


class OfficeConvertError(Exception):
    pass


def soffice_available() -> bool:
    return shutil.which("soffice") is not None or shutil.which("libreoffice") is not None


def images_to_pdf_bytes(images: List[bytes]) -> bytes:
    """One or more raster images -> a single PDF (one page per image, in order)."""
    if not images:
        raise OfficeConvertError("No image data provided.")
    frames = []
    for raw in images:
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as exc:
            raise OfficeConvertError(f"Could not read image: {exc}")
        # PDF has no alpha channel / palette support — flatten onto white.
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, "white")
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        frames.append(img)

    out = io.BytesIO()
    frames[0].save(out, format="PDF", save_all=True, append_images=frames[1:])
    return out.getvalue()


def docx_to_pdf_bytes(data: bytes, filename_hint: str = "document.docx") -> bytes:
    """One DOCX -> PDF bytes, via headless LibreOffice."""
    if not soffice_available():
        raise OfficeConvertError(
            "Word-to-PDF conversion is not available on this server "
            "(LibreOffice is not installed)."
        )
    binary = shutil.which("soffice") or shutil.which("libreoffice")

    workdir = Path(tempfile.mkdtemp(prefix="docx2pdf_"))
    profile_dir = workdir / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(filename_hint).stem[:80] or "document"
    in_path = workdir / f"{stem}.docx"
    try:
        in_path.write_bytes(data)
        proc = subprocess.run(
            [
                binary,
                "--headless",
                "--nologo",
                "--nofirststartwizard",
                "--norestore",
                f"-env:UserInstallation=file://{profile_dir}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(workdir),
                str(in_path),
            ],
            capture_output=True,
            timeout=_SOFFICE_TIMEOUT_SECONDS,
        )
        out_path = workdir / f"{stem}.pdf"
        if proc.returncode != 0 or not out_path.exists():
            detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            raise OfficeConvertError(
                f"Could not convert '{filename_hint}' to PDF."
                + (f" ({detail[:300]})" if detail else "")
            )
        return out_path.read_bytes()
    except subprocess.TimeoutExpired:
        raise OfficeConvertError(
            f"Converting '{filename_hint}' to PDF timed out — the document may be "
            "too complex or contain a macro/link that hung the converter."
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def page_count(pdf_bytes: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
