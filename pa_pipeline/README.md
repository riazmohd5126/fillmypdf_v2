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

## Specialty pipeline: rheumatology + dermatology

A separate, standalone harvest/profile pair aimed at these two specialties,
where variety (drug class, condition, payer, structural type) is the
explicit priority — not raw form count. Feeds the same downstream steps
(`pa_schema_extractor.py`, `pa_vision_mapper.py`, `pa_stratify.py`) once
pointed at its output folder.

```
pa_rheum_derm_taxonomy.py  -> shared drug/class/condition/payer data (pure, no I/O)
pa_rheum_derm_harvester.py -> pa_forms_rheum_derm/<structural_type>/<specialty>/<drug_class>/<payer>/*.pdf
pa_rheum_derm_profiler.py  -> report_rheum_derm/profile_rheum_derm.csv + coverage report
```

Why not just widen `pa_form_harvester.py`'s drug list: rheum and derm share
a lot of molecules (Humira, Cosentyx, Otezla, Rinvoq, Stelara treat both,
just asking different clinical questions — joint counts vs. PASI/BSA), and
a flat drug x payer cross product either buries rare mechanisms under
Humira-x-every-payer results or exhausts the first few drugs alphabetically
under a query cap. The specialty harvester instead classifies every drug by
mechanism (TNF, IL-17, IL-23, JAK, PDE4, topical, ...) and condition, builds
condition-level + drug-level + drug-x-rotating-payer dorks, then
**round-robins the final query list across drug class/specialty** so even a
small `--max-queries` cap samples every mechanism. `--fill-gaps` re-sorts a
follow-up run to spend its budget on whatever (specialty, drug class) cell
is still thinnest.

```bash
# Broad first pass, variety-first ordering, capped query budget:
python3 pa_pipeline/pa_rheum_derm_harvester.py --discover --max-queries 80

# Rheumatology only, wider payer rotation per drug:
python3 pa_pipeline/pa_rheum_derm_harvester.py --discover --specialty rheum --payers-per-drug 8

# Follow-up run that prioritizes whatever's still thin:
python3 pa_pipeline/pa_rheum_derm_harvester.py --discover --fill-gaps --max-queries 60

# See the query plan first — no API calls, no downloads:
python3 pa_pipeline/pa_rheum_derm_harvester.py --discover --dry-run

# Coverage report: by specialty, drug class, payer, structural type, plus
# rheum/derm clinical-question coverage (PASI/BSA, DAS28/CDAI, TB/hep
# screening, conventional-DMARD step therapy):
python3 pa_pipeline/pa_rheum_derm_profiler.py --root pa_forms_rheum_derm --out report_rheum_derm
```

`pa_rheum_derm_profiler.py` extends `pa_profiler.py`'s `SEMANTIC` dict in
place (imports it, doesn't fork it) with the taxonomy's `EXTRA_SEMANTIC`
tags, so its per-PDF profiling logic stays a single source of truth.

## Canonical model

The canonical field catalog lives in `fillmypdf/models/pa_canonical.py`.
`pa_schema_extractor.py` derives its `CANON` table from this catalog so both
agree on field names, entities, semantic types, and critical-field flags.

Critical fields (wrong value = denial): `patient.last_name`, `patient.dob`,
`insurance.member_id`, `prescriber.npi`, `medication.drug_name`,
`medication.ndc`, `clinical.primary_diagnosis_code`.
