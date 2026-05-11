# FillMyPDF API — Debian slim + Poppler for pdf2image / PDF pipelines
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY fillmypdf/ ./fillmypdf/

RUN mkdir -p fillmypdf/storage/temp/uploads \
             fillmypdf/storage/temp/outputs \
             fillmypdf/storage/profiles \
             fillmypdf/storage/jobs

EXPOSE 8000

CMD ["uvicorn", "fillmypdf.main:app", "--host", "0.0.0.0", "--port", "8000"]
