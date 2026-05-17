# FillMyPDF — Zapier Platform integration

Private [Zapier Platform CLI](https://platform.zapier.com/cli_docs/cli) app that calls your **FillMyPDF** FastAPI backend (`X-API-Key` auth). Use it to build Zaps that **queue async batch/template fills** and **poll job status**.

## What's included

| Type | Key | Meaning |
|------|-----|--------|
| **Authentication** | Custom | API **base URL** (no trailing slash) + FillMyPDF **API key**. Connection test uses `GET /api/v1/templates`. |
| **Search** | `template` | Lists library templates — use earlier in Zap; map **`id`** into **Template ID** on submit actions. |
| **Action** | `submit_template_batch_job` | `POST /api/v1/jobs/template-batch` (stored `template_id` + JSON `records`). |
| **Action** | `submit_pdf_batch_job` | Downloads a PDF from a **public HTTPS URL**, then `POST /api/v1/jobs/batch`. |
| **Action** | `get_job_status` | `GET /api/v1/jobs/{job_id}` — use after submit; exposes `download_url_absolute` when `status` is `done`. |

FillMyPDF **`webhook_url`** on job APIs can point at **Zapier Catch Hook** so a Zap starts when the job completes (no extra trigger in this app).

## Prerequisites

- **Node.js 18+**
- Backend reachable from the internet if Zaps run in Zapier Cloud (localhost only works with local invoke during dev).
- **Two different secrets** (often confused):

  1. **FillMyPDF API key** — stored in the Zapier connection (`X-API-Key`).
  2. **`mapping_llm_token`** — OpenAI-compat / Gemini token FillMyPDF uses as multipart field `ai_api_key` (stored as a Zapier secret field).

## Local workflow

```bash
cd integrations/zapier
npm install
npm run validate      # zapier validate
```

Register and deploy (first time):

```bash
npx zapier login          # browser OAuth
npx zapier register       # pick a Zapier integration name if prompted
npx zapier push           # upload
```

Iterate with `npx zapier push` after code changes.

## Dependency note

CLI requires an **exact** `zapier-platform-core` version in `dependencies` (see `package.json`).

## Troubleshooting

- **401** — Wrong FillMyPDF key or bad `base_url` (must include scheme, e.g. `https://api.example.com`).
- **PDF URL action fails** — URL must `GET` a raw PDF; short-lived tokens may expire before the Zap runs.
- **400 on submit** — `records` must be a JSON **array** string with at least one object (same as the REST API).

## Docs

OpenAPI UI: `{base_url}/docs`
