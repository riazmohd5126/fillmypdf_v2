# PA Pipeline — Offline Form Mapping

Builds a **per-form canonical field map** from blank PA PDFs once, with no PHI.
Runtime fill reads the stored map from `pa_forms.db` instead of calling an LLM
on every request.

## Architecture

```
pa_form_harvester.py   -> pa_forms/<type>/<payer>/*.pdf
pa_profiler.py         -> report/profile.csv, clusters.csv
pa_stratify.py         -> test_set/<type>/<payer>/*.pdf  (deduped)
pa_schema_extractor.py -> schema_out/pa_forms.db, field_alias_map.csv
pa_vision_mapper.py    -> pa_forms.db updated (vision-matched leftovers)
```

Runtime integration:
- `fillmypdf/models/pa_canonical.py` — unified canonical model (PARequest, CATALOG, resolve_label)
- `fillmypdf/services/pa_map_store.py` — read maps from pa_forms.db
- `fillmypdf/services/pa_fill_service.py` — fill from PARequest using stored maps

## Setup

```bash
pip install "pypdf[crypto]" pymupdf anthropic
# For vision mapper only:
export ANTHROPIC_API_KEY=...
```

## Run Order

### Step 1 — Profile the corpus

```bash
python3 pa_pipeline/pa_profiler.py \
    --root /Users/riazmohd/Downloads/test_set \
    --out pa_pipeline/report
```

Outputs: `report/profile.csv`, `report/clusters.csv` + console coverage report.

### Step 2 — Extract field->canonical map (name-matching pass)

```bash
python3 pa_pipeline/pa_schema_extractor.py \
    --root /Users/riazmohd/Downloads/test_set \
    --out pa_pipeline/schema_out
```

Outputs: `schema_out/pa_forms.db`, `schema_out/field_alias_map.csv`, `schema_out/canonical_schema.json`.

**Review `field_alias_map.csv`**: confirm high-confidence auto-mappings, fix UNMAPPED
and medium-confidence rows. This is the most valuable human step.

### Step 3 — Stratify into a minimal test set (already done)

`/Users/riazmohd/Downloads/test_set` was already produced by `pa_stratify.py`.
Re-run if you add new forms:

```bash
python3 pa_pipeline/pa_stratify.py \
    --profile pa_pipeline/report/profile.csv \
    --root /Users/riazmohd/Downloads \
    --out pa_pipeline/test_set_new
```

### Step 4 — Vision pass (resolve low/none-confidence fields)

Blank forms only, no PHI. Renders each page with unresolved fields and asks
Claude to read the printed label by pixel position.

```bash
# Smoke test first (5 forms, ~$0.05):
python3 pa_pipeline/pa_vision_mapper.py \
    --db pa_pipeline/schema_out/pa_forms.db \
    --limit 5

# Full run:
python3 pa_pipeline/pa_vision_mapper.py \
    --db pa_pipeline/schema_out/pa_forms.db
```

### Step 5 — Golden-patient eval

```bash
# Smoke (20 forms):
python3 pa_pipeline/pa_eval.py \
    --db pa_pipeline/schema_out/pa_forms.db \
    --test-set /Users/riazmohd/Downloads/test_set \
    --category acroform \
    --limit 20

# Full acroform run:
python3 pa_pipeline/pa_eval.py \
    --db pa_pipeline/schema_out/pa_forms.db \
    --test-set /Users/riazmohd/Downloads/test_set \
    --category acroform

open pa_pipeline/eval_out/report.csv
```

## Artifacts (git-ignored)

| Path | Contents |
|------|----------|
| `pa_pipeline/report/` | profile.csv, clusters.csv |
| `pa_pipeline/schema_out/` | pa_forms.db, field_alias_map.csv, canonical_schema.json |
| `pa_pipeline/eval_out/` | filled PDFs, report.csv, summary.json |

## Canonical model

The canonical field catalog lives in `fillmypdf/models/pa_canonical.py`.
`pa_schema_extractor.py` derives its `CANON` table from this catalog so both
agree on field names, entities, semantic types, and critical-field flags.

Critical fields (wrong value = denial): `patient.last_name`, `patient.dob`,
`insurance.member_id`, `prescriber.npi`, `medication.drug_name`,
`medication.ndc`, `clinical.primary_diagnosis_code`.
