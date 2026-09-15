# FillMyPDF API — Debian slim + Poppler (PDF raster) + Tesseract (card OCR)
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY fillmypdf/ ./fillmypdf/

RUN mkdir -p fillmypdf/storage/temp/uploads \
             fillmypdf/storage/temp/outputs \
             fillmypdf/storage/profiles \
             fillmypdf/storage/jobs \
             fillmypdf/storage/audit

EXPOSE 8000

# Render sets PORT; default 8000 for local Docker.
#
# --forwarded-allow-ips='*': Render's edge terminates TLS and forwards plain
# HTTP to this container from its own internal proxy IP, not 127.0.0.1 (the
# uvicorn default). Without this, uvicorn ignores the X-Forwarded-Proto:
# https header Render sends, so request.url.scheme reads "http" — and any
# redirect Starlette builds from it (e.g. its automatic add-a-trailing-slash
# redirect) points at an insecure http:// URL. A browser on an https:// page
# then silently blocks following that redirect as mixed content, surfacing
# to client JS as an unexplained "Failed to fetch" with no HTTP status at
# all. The container has no other ingress path than Render's own proxy, so
# trusting every peer here is safe.
CMD ["sh", "-c", "uvicorn fillmypdf.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
