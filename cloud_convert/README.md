# FillMyPDF Cloud Converter

The **only** place the commonforms/FFDNet (YOLO) model runs. Turns a flat
(non-fillable) PDF into a fillable AcroForm and returns it. Thin clients — the
FillMyPDF app, the Chrome extension, and any other app — offload this heavy step
here so they never need torch/GPU locally.

**PHI note:** clients send only the **blank form** for conversion. All field
mapping and value filling happen client-side afterward, so no patient data
reaches this service.

## Why a separate service
`commonforms.prepare_form` runs YOLO inference on *all pages at once*, which
spikes memory past 8 GB on multi-page docs. `converter.py` here runs inference
**one page at a time** (peak memory bounded by a single page) and caches the
model per process.

## Endpoints
- `GET /health` → status + model info.
- `POST /convert` (multipart `file=<pdf>`, header `X-Convert-Key: <key>`)
  → returns the fillable PDF (`application/pdf`, `X-Fields-Detected` header).

## Run locally
```bash
cd cloud_convert
pip install -r requirements.txt
CONVERT_API_KEY=dev-secret uvicorn app:app --host 0.0.0.0 --port 8080
# test:
curl -s -H "X-Convert-Key: dev-secret" -F "file=@flat.pdf" \
     http://localhost:8080/convert -o flat_fillable.pdf
```

## Run with Docker
```bash
cd cloud_convert
docker build -t fillmypdf-converter .
docker run -p 8080:8080 -e CONVERT_API_KEY=prod-secret fillmypdf-converter
```

## Deploy to Render
Use the **Docker** runtime (native Python would download the model at runtime).

1. Push this repo to GitHub/GitLab.
2. Render → **New + → Blueprint**, select the repo; it reads `cloud_convert/render.yaml`.
   (Or **New + → Web Service**, Root Directory = `cloud_convert`, Runtime = Docker.)
3. **Pick an instance with real RAM.** Free/Starter (512 MB) will OOM on the YOLO
   model — use **Standard (2 GB)** or larger; use a GPU/large plan for `FFDNet-L`.
4. Render sets `$PORT` automatically (the Dockerfile binds to it). Health check
   is `/health`.
5. Grab the generated `CONVERT_API_KEY` (Environment tab) and the service URL,
   then wire the main app:
   ```
   COMMONFORMS_MODE=cloud
   CONVERT_SERVICE_URL=https://fillmypdf-converter.onrender.com/convert
   CONVERT_SERVICE_KEY=<the generated key>
   ```

Notes:
- Cold starts: on plans that spin down, the first request loads the model and is
  slow; the image bakes the model in so no Hugging Face access is needed at run.
- Bump `CONVERT_SERVICE_TIMEOUT` in the app if cold starts exceed 120s.

## Configuration (env vars)
| Var | Default | Notes |
|-----|---------|-------|
| `CONVERT_API_KEY` | (unset) | Required key for `X-Convert-Key`; unset = no auth (dev only) |
| `COMMONFORMS_MODEL` | `FFDNet-S` | `FFDNet-S` (small) or `FFDNet-L` (accurate) |
| `COMMONFORMS_FAST` | `true` | ONNX CPU path (lower memory); needs `onnxruntime` |
| `COMMONFORMS_IMAGE_SIZE` | `1024` | Higher = more accurate + more memory |
| `COMMONFORMS_CONFIDENCE` | `0.1` | Detection threshold |
| `COMMONFORMS_DEVICE` | `cpu` | Set to a CUDA index (e.g. `0`) on GPU boxes |
| `CONVERT_MAX_PAGES` | `40` | Hard cap; extra pages skipped |
| `CONVERT_MAX_UPLOAD_MB` | `25` | Reject larger uploads |

## Wire the main app to this service
In the FillMyPDF app's `.env` (the thin client):
```
COMMONFORMS_MODE=cloud
CONVERT_SERVICE_URL=https://<your-aws-host>/convert
CONVERT_SERVICE_KEY=prod-secret
```
The app's `PDFService.convert_to_fillable` will POST flat PDFs here and use the
returned fillable; acroform forms still convert locally (no service call). If the
service is unreachable, the app falls back to copying the PDF as-is (0 fields)
rather than running torch on the client.

## Sizing / scaling
- One `uvicorn` worker per container (peak RAM ≈ one model + one page).
- Scale horizontally with replicas behind a load balancer.
- `FFDNet-S` + `fast=true` runs comfortably on a small CPU instance; use
  `FFDNet-L` + a GPU (`COMMONFORMS_DEVICE=0`) for best accuracy.
