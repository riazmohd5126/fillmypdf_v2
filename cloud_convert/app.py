"""
Cloud converter service — flat PDF -> fillable AcroForm.

The ONLY place the commonforms/torch (YOLO) model runs. Thin clients (the
FillMyPDF app, Chrome extension, other apps) POST a blank form here and get back
a fillable PDF; all mapping/filling happens client-side afterward, so no patient
values (PHI) are ever sent to this service.

Run:
    CONVERT_API_KEY=... uvicorn app:app --host 0.0.0.0 --port 8080

Auth: send the key as the ``X-Convert-Key`` header (skipped if CONVERT_API_KEY
is unset — dev/private-network only).
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import converter

API_KEY = os.getenv("CONVERT_API_KEY", "").strip()
MAX_UPLOAD_MB = int(os.getenv("CONVERT_MAX_UPLOAD_MB", "25"))

app = FastAPI(title="FillMyPDF Cloud Converter", version="1.0.0")


def _check_key(x_convert_key: str | None) -> None:
    if API_KEY and (x_convert_key or "").strip() != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Convert-Key")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "model": os.getenv("COMMONFORMS_MODEL", "FFDNet-S"),
        "fast": os.getenv("COMMONFORMS_FAST", "true"),
        "image_size": os.getenv("COMMONFORMS_IMAGE_SIZE", "1024"),
        "auth": bool(API_KEY),
    })


@app.post("/convert")
async def convert_endpoint(
    file: UploadFile = File(..., description="Flat (non-fillable) PDF"),
    x_convert_key: str | None = Header(default=None),
):
    """Detect + inject form fields into a flat PDF; return the fillable PDF."""
    _check_key(x_convert_key)

    data = await file.read()
    if not data[:5] == b"%PDF-":
        raise HTTPException(status_code=400, detail="Uploaded file is not a PDF")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"PDF exceeds {MAX_UPLOAD_MB} MB")

    tmp = Path(tempfile.gettempdir()) / f"cf_{uuid.uuid4().hex}"
    in_path = tmp.with_suffix(".in.pdf")
    out_path = tmp.with_suffix(".out.pdf")
    try:
        in_path.write_bytes(data)
        try:
            n = converter.convert(in_path, out_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Conversion failed: {exc}")
        if not out_path.exists():
            raise HTTPException(status_code=500, detail="Converter produced no output")
        return FileResponse(
            str(out_path),
            media_type="application/pdf",
            filename=(file.filename or "form").rsplit(".", 1)[0] + "_fillable.pdf",
            headers={"X-Fields-Detected": str(n)},
        )
    finally:
        in_path.unlink(missing_ok=True)
        # out_path is streamed by FileResponse; best-effort cleanup afterward is
        # handled by the OS temp dir. (Left in place so the response can read it.)
