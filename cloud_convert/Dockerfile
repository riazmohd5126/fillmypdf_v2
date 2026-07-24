FROM python:3.11-slim

# System libs commonforms/pypdfium2/opencv need at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY converter.py app.py ./

# Pre-download the model at build time so the first request is fast and the
# running container needs no outbound Hugging Face access.
ARG COMMONFORMS_MODEL=FFDNet-S
ARG COMMONFORMS_FAST=true
ENV COMMONFORMS_MODEL=${COMMONFORMS_MODEL} \
    COMMONFORMS_FAST=${COMMONFORMS_FAST} \
    COMMONFORMS_IMAGE_SIZE=1024 \
    HF_HUB_OFFLINE=0
RUN python -c "from converter import _get_detector; _get_detector(); print('model cached')" || true

ENV COMMONFORMS_IMAGE_SIZE=1024 \
    CONVERT_MAX_PAGES=40 \
    CONVERT_MAX_UPLOAD_MB=25 \
    HF_HUB_OFFLINE=1

EXPOSE 8080
# Single worker keeps peak RAM to one model + one page in flight. Scale out with
# replicas behind a load balancer rather than many workers per container.
# Shell form so hosts that inject $PORT (Render, Cloud Run, Heroku) are honored;
# falls back to 8080 locally.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
