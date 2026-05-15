# FillMyPDF feature matrix → implementation route

This repo mirrors the Cowork JSX **Feature Matrix**. Canonical data is **`docs/feature-matrix.json`** (regenerate via `python3 scripts/feature_matrix_snapshot.py`).  
The React viewer is **`docs/FeatureMatrix.jsx`** (bundler must allow JSON imports; Tailwind assumed).

The **intent** remains from your product notes: PDF-driven UX, reusable profiles, avoid duplicating CommonForms/Gemini work in the frontend, prioritise APIs before heavyweight workflow builders.

---

## Phase alignment vs this backend

| Phase (matrix) | Theme | Repo reality today |
|----------------|-------|--------------------|
| **0** | Core vision | Done: static→fillable path, Gemini vision mapping, batch/templates |
| **1** | Foundation | Done on API side: REST surface under `/api/v1/*`, encrypted profiles, template library manifests, `/usage`; **dashboard** stays product/UI—not this FastAPI tree |
| **2** | Automation | Done: CSV/JSON/XLSX bulk (sync), async **`/jobs`**, extract jobs, outbound **webhooks** + HMAC + retries + manual webhook replay; **Zapier** = partial (Catch Hook/workflow—not published app); Chrome extension tracked as **parallel Cowork lane** |

Phases **3–5** (signing bundle, CRM, enterprise SSO, etc.) largely **unchanged roadmap** versus matrix—most are not implemented here yet.

---

## Row-by-row reconcile (snapshot)

Interpretation keyed to **`docs/feature-matrix.json`** `status`/`recommendation` fields:

- **DONE (backend-aligned):** `static-to-fillable`, `ai-autofill`, `public-api`* , `user-profiles`, `form-templates`** , `bulk-fill`, `smart-extraction`*** , `webhooks`
- ***public-api:** paths differ from matrix stub (`POST /api/convert` …); conceptual box is ✅.
- ****form-templates:** matrix imagined tax catalogs; shipped focus is pharma PA manifests—grow categories intentionally.
- ***smart-extraction:** shipped = AcroForm/structured extract; richer “Vision read any scan” is still backlog if you distinguish it later.
- **BUILD / ACTIVE:** `dashboard` (web app), Phase 3 **signing** cluster, **`zapier` + `browser-extension`** (external tracks), OpenAPI parity across non-job routes  
- **LATER:** workflow rule engine, SSO/teams/HIPAA, CRM/Drive/etc.
- **SKIP:** unchanged from matrix rationale

---

## The route we’re following

1. **Stabilise the API narrative** — OpenAPI/schema **examples** on core models (`fillmypdf/models/__init__.py`); **multipart** `Form(..., examples=[...])` on batch + jobs; **Query** examples on sync **extract** + **template list** filters; template **fill/batch** + admin **manifest_json** use shared `openapi_form_examples.py`.  
2. **Dashboard** unifies JSX/HTML prototype—history, API keys UX, billing stub.  
3. **Official Zapier** + **Agents** tooling only after payloads + auth ergonomics freeze (today: generic webhook + REST).  
4. **Signing** milestone after bulk + extract are monetisation-ready—pulls audit + multi-sign dependencies.  

Infra deployments (Docker/Fly/CI) are **orthogonal** gates; defer until Feature Matrix phases 1–2 are boxed on product.
