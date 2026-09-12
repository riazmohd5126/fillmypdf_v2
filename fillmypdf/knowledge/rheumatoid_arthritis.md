# Rheumatoid Arthritis — step-therapy knowledge file

Used by Note Paste to judge "has this patient tried and failed a conventional
DMARD" style prior-authorization criteria from pasted visit notes. Swap this
file (and only this file) to extend Note Paste to another indication — the
extraction contract in `note_paste_service.py` stays fixed.

## Conventional DMARDs that count toward step therapy

- Methotrexate (MTX)
- Leflunomide (Arava)
- Sulfasalazine (Azulfidine)
- Hydroxychloroquine (Plaquenil)

Biologics and JAK inhibitors (adalimumab, etanercept, tofacitinib, etc.) do
**not** count as conventional DMARDs for step-therapy purposes — a note
mentioning one of these as a *prior* therapy still means the conventional
step wasn't satisfied unless a conventional DMARD is *also* documented.

## Optimized dose thresholds

| Drug | Optimized dose |
|---|---|
| Methotrexate | ≥ 15 mg/week (oral or subcutaneous) |
| Leflunomide | 20 mg/day |
| Sulfasalazine | 2 g/day (2000 mg/day, typically split doses) |
| Hydroxychloroquine | 400 mg/day (or per weight-based dosing, ~5 mg/kg/day) |

A course only counts toward "adequate trial at optimized dose" from the date
the dose reached this threshold, not from the date the drug was first
prescribed at a lower starting dose.

## What does NOT count as a start date

Do not treat any of the following as a therapy start:

- "Rx sent" / "prescription sent to pharmacy"
- "discussed initiation" / "plan to start"
- "will start when ready" / "patient to begin once insurance approved"
- A refill or renewal mention with no earlier documented start

If notes only show one of these phrasings and no later note confirms the
patient actually took the drug, the answer is `not_in_notes` for that drug —
never infer a start date from an intent-to-prescribe statement.

## Failure language

Treat the following as documentation that the trial failed (use the
patient's own words / the clinician's words as the evidence quote, do not
paraphrase into this list):

- "inadequate response"
- "minimal benefit"
- "denies improvement"
- "no significant improvement"
- "discontinued due to lack of efficacy"
- "intolerable side effects" / "discontinued due to [side effect]" (counts as
  a failed trial for step-therapy purposes even though it isn't an efficacy
  failure — say so in the summary)

## Search rule

Search **every** DMARD mentioned anywhere in the pasted notes, not just the
first one found. A patient may have tried and failed methotrexate six months
ago and sulfasalazine more recently — report the most recent one that
reached optimized dose for the requested duration, but flag in `summary`
if more than one DMARD trial is documented.

## Ambiguous cases — always flag, never resolve silently

- Total time on the drug meets the duration criterion, but time *at the
  optimized dose* does not → `confidence: "review_required"`, explain the
  gap in `summary`.
- Contradictory dates across notes for the same drug → `review_required`.
- Heavy hedging/negation language ("possibly", "reports some improvement but
  unclear if related") → `review_required` rather than guessing yes/no.
