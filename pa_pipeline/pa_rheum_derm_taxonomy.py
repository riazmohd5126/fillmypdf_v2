"""
pa_rheum_derm_taxonomy.py — shared drug/payer/condition taxonomy for the
rheumatology + dermatology PA pipeline.

Pure data, no side effects, no I/O — safe to import from both the harvester
and the profiler (same pattern as canonical_model.py feeding
pa_schema_extractor.py).

Why a separate taxonomy from pa_form_harvester.py's DRUG_TERMS/PAYER_DOMAINS:
that list is a flat, generic "specialty drug" grab-bag. Rheum and derm PA
forms have their own decisive axes an autofill engine needs to be tested
against:
  - drug MECHANISM/CLASS (TNF vs IL-17 vs IL-23 vs JAK vs PDE4 vs topical...)
    because the step-therapy question set differs by class, not by brand.
  - CONDITION (RA vs PsA vs AS vs plaque psoriasis vs atopic dermatitis...)
    because payers often have a per-indication form for the same drug.
  - SPECIALTY overlap ("both") — Humira/Cosentyx/Otezla/Rinvoq/Stelara etc.
    are prescribed by both rheumatology and dermatology for different
    indications, and the two specialties' forms ask different clinical
    questions (joint counts vs BSA/PASI) for the same molecule.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# DRUGS — brand -> {generic, class, specialty, conditions}
# specialty: "rheum" | "derm" | "both"
# ---------------------------------------------------------------------------

RHEUM_DRUGS: dict[str, dict] = {
    "Humira":     {"generic": "adalimumab",        "class": "TNF-inhibitor",   "specialty": "both",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "ankylosing spondylitis",
                                  "hidradenitis suppurativa", "plaque psoriasis"]},
    "Enbrel":     {"generic": "etanercept",         "class": "TNF-inhibitor",   "specialty": "both",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "ankylosing spondylitis",
                                  "plaque psoriasis"]},
    "Cimzia":     {"generic": "certolizumab pegol",  "class": "TNF-inhibitor",   "specialty": "both",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "ankylosing spondylitis",
                                  "plaque psoriasis"]},
    "Simponi":    {"generic": "golimumab",           "class": "TNF-inhibitor",   "specialty": "rheum",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "ankylosing spondylitis"]},
    "Simponi Aria": {"generic": "golimumab (IV)",    "class": "TNF-inhibitor",   "specialty": "rheum",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "ankylosing spondylitis"]},
    "Remicade":   {"generic": "infliximab",          "class": "TNF-inhibitor",   "specialty": "both",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "ankylosing spondylitis",
                                  "plaque psoriasis"]},
    "Inflectra":  {"generic": "infliximab-dyyb",     "class": "TNF-inhibitor",   "specialty": "both",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "plaque psoriasis"]},
    "Renflexis":  {"generic": "infliximab-abda",     "class": "TNF-inhibitor",   "specialty": "both",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "plaque psoriasis"]},
    "Avsola":     {"generic": "infliximab-axxq",     "class": "TNF-inhibitor",   "specialty": "both",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "plaque psoriasis"]},
    "Actemra":    {"generic": "tocilizumab",         "class": "IL-6-inhibitor",  "specialty": "rheum",
                   "conditions": ["rheumatoid arthritis", "giant cell arteritis", "juvenile idiopathic arthritis"]},
    "Kevzara":    {"generic": "sarilumab",           "class": "IL-6-inhibitor",  "specialty": "rheum",
                   "conditions": ["rheumatoid arthritis"]},
    "Kineret":    {"generic": "anakinra",            "class": "IL-1-inhibitor",  "specialty": "rheum",
                   "conditions": ["rheumatoid arthritis"]},
    "Ilaris":     {"generic": "canakinumab",         "class": "IL-1-inhibitor",  "specialty": "rheum",
                   "conditions": ["periodic fever syndrome", "gout flare"]},
    "Orencia":    {"generic": "abatacept",           "class": "T-cell-costim",   "specialty": "rheum",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "juvenile idiopathic arthritis"]},
    "Rituxan":    {"generic": "rituximab",           "class": "CD20-inhibitor",  "specialty": "rheum",
                   "conditions": ["rheumatoid arthritis", "granulomatosis with polyangiitis"]},
    "Xeljanz":    {"generic": "tofacitinib",         "class": "JAK-inhibitor",   "specialty": "rheum",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "ankylosing spondylitis"]},
    "Rinvoq":     {"generic": "upadacitinib",        "class": "JAK-inhibitor",   "specialty": "both",
                   "conditions": ["rheumatoid arthritis", "psoriatic arthritis", "ankylosing spondylitis",
                                  "atopic dermatitis"]},
    "Olumiant":   {"generic": "baricitinib",         "class": "JAK-inhibitor",   "specialty": "both",
                   "conditions": ["rheumatoid arthritis", "alopecia areata"]},
    "Cosentyx":   {"generic": "secukinumab",         "class": "IL-17-inhibitor", "specialty": "both",
                   "conditions": ["psoriatic arthritis", "ankylosing spondylitis", "plaque psoriasis",
                                  "hidradenitis suppurativa"]},
    "Taltz":      {"generic": "ixekizumab",          "class": "IL-17-inhibitor", "specialty": "both",
                   "conditions": ["psoriatic arthritis", "ankylosing spondylitis", "plaque psoriasis"]},
    "Bimzelx":    {"generic": "bimekizumab",         "class": "IL-17-inhibitor", "specialty": "both",
                   "conditions": ["psoriatic arthritis", "ankylosing spondylitis", "plaque psoriasis",
                                  "hidradenitis suppurativa"]},
    "Tremfya":    {"generic": "guselkumab",          "class": "IL-23-inhibitor", "specialty": "both",
                   "conditions": ["psoriatic arthritis", "plaque psoriasis"]},
    "Skyrizi":    {"generic": "risankizumab",        "class": "IL-23-inhibitor", "specialty": "both",
                   "conditions": ["psoriatic arthritis", "plaque psoriasis"]},
    "Otezla":     {"generic": "apremilast",          "class": "PDE4-inhibitor",  "specialty": "both",
                   "conditions": ["psoriatic arthritis", "plaque psoriasis", "oral ulcers of Behcet's disease"]},
    "Krystexxa":  {"generic": "pegloticase",         "class": "uricase",         "specialty": "rheum",
                   "conditions": ["refractory chronic gout"]},
    "Benlysta":   {"generic": "belimumab",           "class": "BLyS-inhibitor",  "specialty": "rheum",
                   "conditions": ["systemic lupus erythematosus", "lupus nephritis"]},
    "Saphnelo":   {"generic": "anifrolumab",         "class": "IFNAR-antagonist", "specialty": "rheum",
                   "conditions": ["systemic lupus erythematosus"]},
}

DERM_DRUGS: dict[str, dict] = {
    "Stelara":    {"generic": "ustekinumab",         "class": "IL-12/23-inhibitor", "specialty": "both",
                   "conditions": ["plaque psoriasis", "psoriatic arthritis"]},
    "Ilumya":     {"generic": "tildrakizumab",       "class": "IL-23-inhibitor", "specialty": "derm",
                   "conditions": ["plaque psoriasis"]},
    "Siliq":      {"generic": "brodalumab",          "class": "IL-17RA-inhibitor", "specialty": "derm",
                   "conditions": ["plaque psoriasis"]},
    "Dupixent":   {"generic": "dupilumab",           "class": "IL-4Ra-inhibitor", "specialty": "derm",
                   "conditions": ["atopic dermatitis", "prurigo nodularis", "chronic spontaneous urticaria"]},
    "Adbry":      {"generic": "tralokinumab",        "class": "IL-13-inhibitor", "specialty": "derm",
                   "conditions": ["atopic dermatitis"]},
    "Cibinqo":    {"generic": "abrocitinib",         "class": "JAK-inhibitor",   "specialty": "derm",
                   "conditions": ["atopic dermatitis"]},
    "Opzelura":   {"generic": "ruxolitinib cream",   "class": "topical-JAK",     "specialty": "derm",
                   "conditions": ["atopic dermatitis", "vitiligo"]},
    "Vtama":      {"generic": "tapinarof cream",     "class": "topical-AhR-agonist", "specialty": "derm",
                   "conditions": ["plaque psoriasis"]},
    "Zoryve":     {"generic": "roflumilast",         "class": "topical-PDE4",    "specialty": "derm",
                   "conditions": ["plaque psoriasis", "seborrheic dermatitis"]},
    "Eucrisa":    {"generic": "crisaborole",         "class": "topical-PDE4",    "specialty": "derm",
                   "conditions": ["atopic dermatitis"]},
    "Xolair":     {"generic": "omalizumab",          "class": "anti-IgE",        "specialty": "derm",
                   "conditions": ["chronic spontaneous urticaria"]},
    "Litfulo":    {"generic": "ritlecitinib",        "class": "JAK3/TEC-inhibitor", "specialty": "derm",
                   "conditions": ["alopecia areata"]},
}

# Merge, letting an entry present in both dicts win as "both" (RHEUM_DRUGS
# already marks the true overlaps as "both"; DERM_DRUGS only adds derm-only
# drugs, so a straight update is safe and keeps a single source of truth).
ALL_DRUGS: dict[str, dict] = {**RHEUM_DRUGS, **DERM_DRUGS}

DRUG_CLASSES = sorted({d["class"] for d in ALL_DRUGS.values()})

# ---------------------------------------------------------------------------
# CONDITIONS — the per-indication axis payers split forms on
# ---------------------------------------------------------------------------

RHEUM_CONDITIONS = [
    "rheumatoid arthritis", "psoriatic arthritis", "ankylosing spondylitis",
    "axial spondyloarthritis", "juvenile idiopathic arthritis",
    "systemic lupus erythematosus", "giant cell arteritis", "gout",
]

DERM_CONDITIONS = [
    "plaque psoriasis", "atopic dermatitis", "hidradenitis suppurativa",
    "chronic spontaneous urticaria", "alopecia areata", "vitiligo",
    "prurigo nodularis",
]

# ---------------------------------------------------------------------------
# PAYERS — generic payer domains (kept in sync with pa_form_harvester.py)
# plus the specialty-pharmacy / specialty-PA processors that actually route
# most rheum/derm biologic PAs (buy-and-bill and pharmacy benefit alike).
# ---------------------------------------------------------------------------

PAYER_DOMAINS = [
    "uhcprovider.com", "aetna.com", "cigna.com", "humana.com", "anthem.com",
    "highmark.com", "floridablue.com", "bcbs.com", "bcbsil.com", "bcbstx.com",
    "carefirst.com", "amerihealth.com", "ibx.com", "wellsense.org",
    "fideliscare.org", "superiorhealthplan.com", "pahealthwellness.com",
    "providers.anthem.com", "molinahealthcare.com", "centene.com",
    "healthnet.com", "kaiserpermanente.org", "cvs.com", "express-scripts.com",
    "optumrx.com", "primetherapeutics.com",
]

SPECIALTY_PHARMACY_DOMAINS = [
    "accredo.com", "cvsspecialty.com", "covermymeds.com", "magellanrx.com",
    "medimpact.com", "empireblue.com", "wellcare.com", "bcbsm.com",
    "regence.com", "bluecrossma.org", "capbluecross.com", "excellusbcbs.com",
]

SPECIALTY_PAYERS = list(dict.fromkeys(PAYER_DOMAINS + SPECIALTY_PHARMACY_DOMAINS))

# Form-shaped phrasing to pair with a payer/drug/condition in a dork query.
SPECIALTY_FORM_TERMS = [
    '"prior authorization request form"',
    '"prior authorization" fax form',
    '"specialty pharmacy" prior authorization form',
    '"biologic" prior authorization form',
    '"step therapy" exception form',
    'prior authorization form',
]

# ---------------------------------------------------------------------------
# SEMANTIC TAGS — additions to pa_profiler.py's SEMANTIC dict. These are the
# clinical concepts that are specific to rheum/derm PA questionnaires
# (disease-activity scores, biologic screening labs, step-therapy-on-a-
# conventional-agent) and mostly absent from a generic PA form profile.
# ---------------------------------------------------------------------------

EXTRA_SEMANTIC: dict[str, list[str]] = {
    "disease_activity_score": ["das28", "cdai", "sdai", "basdai", "asdas", "haq-di", "haq di"],
    "psoriasis_severity": ["pasi", "bsa", "body surface area", "dlqi", "iga score", "investigator global assessment"],
    "tb_screening": ["tuberculosis", "tb test", "ppd", "quantiferon", "igra", "latent tb"],
    "hepatitis_screening": ["hepatitis b", "hepatitis c", "hbsag", "hbv", "hcv"],
    "conventional_dmard": ["methotrexate", "sulfasalazine", "leflunomide", "hydroxychloroquine", "conventional dmard", "csdmard"],
    "biologic_naive": ["biologic naive", "biologic-naive", "prior biologic", "biologic experienced", "first biologic"],
    "joint_count": ["tender joint count", "swollen joint count", "joint count"],
    "site_of_care_biologic": ["self-administer", "self administered", "home infusion", "infusion center", "office administration"],
}
