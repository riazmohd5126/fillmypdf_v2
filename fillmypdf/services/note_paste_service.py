"""
Note Paste Service
===================
Nurse pastes visit notes → drafted clinical-justification answer with quoted
evidence. One model call (reasoning over prose), then a **deterministic**
verification pass that never trusts the model's own arithmetic or claims:

  - every evidence quote must appear verbatim in the text the model actually
    saw (the de-identified text the client sent us) — unverified quotes are
    flagged, never silently dropped;
  - durations (total months on the drug, months at the optimized dose) are
    computed here in Python from the dates the model extracted;
  - a duration mismatch (meets total but not optimized-dose duration) forces
    `review_required` rather than letting the model resolve it silently.

The extraction contract (the JSON shape) stays fixed across indications —
only the knowledge file text changes. See fillmypdf/knowledge/*.md.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from openai import OpenAI

from ..models.note_paste import DrugTrial, EvidenceQuote, NotePasteExtractResponse

_KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

_DEFAULT_QUESTIONS = {
    "rheumatoid_arthritis": (
        "Has the patient tried and failed at least one conventional DMARD "
        "(methotrexate, leflunomide, sulfasalazine, or hydroxychloroquine) "
        "for at least 3 months at an optimized dose, with inadequate "
        "response or intolerance documented?"
    ),
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_whitespace(text: str) -> str:
    """Collapse any run of whitespace (including a line-wrap newline in the
    middle of a sentence — common in pasted chart notes) to a single space.

    The verbatim check below still requires an exact substring match on the
    words themselves — this only makes it whitespace-insensitive, so a model
    that reproduces a quote with normal spacing instead of the source's
    mid-sentence line break isn't wrongly flagged as having fabricated it."""
    return _WHITESPACE_RE.sub(" ", text).strip()


class NotePasteError(Exception):
    pass


class NotePasteService:
    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    # ------------------------------------------------------------------
    # Knowledge file
    # ------------------------------------------------------------------
    @staticmethod
    def load_knowledge(indication: str) -> str:
        safe = re.sub(r"[^a-z0-9_]", "", indication.lower())
        path = _KNOWLEDGE_DIR / f"{safe}.md"
        if not path.is_file():
            raise NotePasteError(
                f"No knowledge file for indication '{indication}'. "
                f"Available: {[p.stem for p in _KNOWLEDGE_DIR.glob('*.md')]}"
            )
        return path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Model call
    # ------------------------------------------------------------------
    def _prompt(self, notes_text: str, question: str, knowledge: str) -> list[dict]:
        system = (
            "You are drafting the clinical-justification section of a prior "
            "authorization request from a nurse's pasted visit notes. Follow "
            "the domain knowledge below exactly. Three rules matter more "
            "than anything else:\n"
            "1. Every quote you return in a trial's `evidence` must appear "
            "VERBATIM (exact substring, same punctuation) in the notes text "
            "you were given. Never paraphrase inside a quote.\n"
            "2. If the answer is not supported by the text, return "
            '`"answer": "not_in_notes"` — never infer or guess.\n'
            "3. Report EVERY conventional DMARD trial you find documented, "
            "not just the first one (see the knowledge file's search rule) "
            "— each as its own entry in `trials`. If `summary` mentions a "
            "drug's outcome, that drug MUST have its own `trials` entry "
            "with a real quote backing it — never describe a drug in the "
            "summary without a matching trial citing evidence for it.\n\n"
            f"--- DOMAIN KNOWLEDGE ---\n{knowledge}\n--- END DOMAIN KNOWLEDGE ---"
        )
        user = (
            f"QUESTION: {question}\n\n"
            f"VISIT NOTES (identifiers already redacted by the client):\n"
            f"---\n{notes_text}\n---\n\n"
            "Respond with STRICT JSON only, no markdown fences, matching "
            "exactly this shape:\n"
            "{\n"
            '  "answer": "yes | no | not_in_notes",\n'
            '  "confidence": "high | medium | review_required",\n'
            '  "summary": "1-3 sentence clinical justification draft covering '
            'every trial below",\n'
            '  "trials": [\n'
            "    {\n"
            '      "drug": "string",\n'
            '      "start_date": "YYYY-MM-DD or null",\n'
            '      "end_date": "YYYY-MM-DD or null",\n'
            '      "optimized_dose_start": "YYYY-MM-DD or null",\n'
            '      "evidence": [{"quote": "verbatim substring from the '
            'notes", "date": "YYYY-MM-DD or null"}]\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "One entry in `trials` per distinct DMARD actually tried — "
            "empty list if none is documented (e.g. answer is not_in_notes "
            "because nothing was ever started).\n"
            "Dates you are not confident about: use null rather than "
            "guessing a year. In particular, if a note gives only a day and "
            "month (e.g. \"3/1\", \"around April\") with no year written "
            "anywhere in the notes, the year is NOT known — return null for "
            "that date rather than assuming the current year or any other "
            "year."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _parse_json(raw: str) -> dict:
        cleaned = (raw or "").strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.lstrip().lower().startswith("json"):
                cleaned = cleaned.lstrip()[4:]
        cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except Exception:
            pass
        # Fall back to the outermost {...} span (handles a stray preamble
        # like "Here is the JSON:" that response_format=json_object should
        # already prevent, but a model can still slip one in).
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except Exception:
                pass
        # Nothing parsed — surface what the model actually sent (truncated)
        # so this is diagnosable from the error response instead of the
        # opaque message this used to raise unconditionally.
        snippet = (raw or "").strip().replace("\n", " ")[:300]
        raise NotePasteError(
            "Model did not return parseable JSON. "
            f"Raw response started with: {snippet!r}"
            if snippet
            else "Model returned an empty response (likely hit the token limit "
            "or was blocked by a safety filter)."
        )

    # ------------------------------------------------------------------
    # Deterministic verification (no model involved)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_date(value: Optional[str]) -> Optional[date]:
        if not value or not _DATE_RE.match(str(value)):
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _verify_trial(self, trial: dict, normalized_source: str) -> DrugTrial:
        """Verify one drug trial's evidence and compute its durations —
        independently of every other trial, so a summary naming several
        drugs can't hide one that has no real backing behind another that
        does."""
        evidence = []
        for ev in trial.get("evidence") or []:
            quote = str((ev or {}).get("quote") or "").strip()
            verified = bool(quote) and _normalize_whitespace(quote) in normalized_source
            evidence.append(
                EvidenceQuote(quote=quote, date=(ev or {}).get("date"), verified=verified)
            )
        # A trial with NO evidence at all is exactly the unbacked-claim case
        # this exists to catch — it's "verified" only if there's at least
        # one quote and every quote given actually checks out.
        all_verified = bool(evidence) and all(ev.verified for ev in evidence)

        start = self._parse_date(trial.get("start_date"))
        end = self._parse_date(trial.get("end_date"))
        opt_start = self._parse_date(trial.get("optimized_dose_start"))
        total_months = round((end - start).days / 30.44, 1) if start and end and end >= start else None
        optimized_months = (
            round((end - opt_start).days / 30.44, 1) if opt_start and end and end >= opt_start else None
        )

        return DrugTrial(
            drug=str(trial.get("drug") or "").strip() or "Unnamed drug",
            start_date=trial.get("start_date"),
            end_date=trial.get("end_date"),
            optimized_dose_start=trial.get("optimized_dose_start"),
            evidence=evidence,
            total_months=total_months,
            optimized_months=optimized_months,
            all_quotes_verified=all_verified,
        )

    def _verify(self, parsed: dict, sent_text: str) -> NotePasteExtractResponse:
        normalized_source = _normalize_whitespace(sent_text)
        raw_trials = parsed.get("trials")
        if not isinstance(raw_trials, list):
            raw_trials = []
        trials = [self._verify_trial(t, normalized_source) for t in raw_trials if isinstance(t, dict)]

        review_flags: list[str] = []
        confidence = parsed.get("confidence") or "review_required"
        any_verified_evidence = False

        for trial in trials:
            verified_count = sum(1 for ev in trial.evidence if ev.verified)
            if verified_count > 0:
                any_verified_evidence = True
            if not trial.all_quotes_verified:
                if verified_count == 0:
                    review_flags.append(
                        f"No verified evidence supports the {trial.drug} trial "
                        f"— nothing in the notes was confirmed to back it up."
                    )
                else:
                    review_flags.append(
                        f"One or more evidence quotes for {trial.drug} could "
                        f"not be verified verbatim against the pasted notes."
                    )
                confidence = "review_required"

            # "Meets total duration but not duration-at-optimized-dose" for
            # THIS drug — never let the model resolve it silently.
            if trial.total_months is not None and trial.optimized_months is not None:
                if trial.total_months >= 3.0 and trial.optimized_months < 3.0:
                    review_flags.append(
                        f"{trial.drug}: total time on therapy ({trial.total_months} mo) "
                        f"meets the 3-month criterion, but time at the optimized "
                        f"dose ({trial.optimized_months} mo) does not — verify "
                        f"before submitting."
                    )
                    confidence = "review_required"

        answer = parsed.get("answer")
        if answer not in ("yes", "no", "not_in_notes"):
            answer = "not_in_notes"
            review_flags.append("Model returned an unrecognized answer value; treated as not_in_notes.")
        if confidence not in ("high", "medium", "review_required"):
            confidence = "review_required"

        # A "yes"/"no" asserted with zero verified evidence ANYWHERE (no
        # trials at all, or every trial unbacked) is exactly the kind of
        # unsupported claim the quote-verification exists to catch. Never
        # let a definitive answer stand on nothing.
        if answer in ("yes", "no") and not any_verified_evidence:
            review_flags.append(
                f"Answer is '{answer}' but no verified evidence quote backs "
                f"it up anywhere — nothing in the notes was confirmed to "
                f"support this conclusion."
            )
            confidence = "review_required"

        return NotePasteExtractResponse(
            answer=answer,
            confidence=confidence,
            summary=str(parsed.get("summary") or ""),
            trials=trials,
            all_quotes_verified=all(t.all_quotes_verified for t in trials),
            review_flags=review_flags,
            sent_text=sent_text,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def extract(
        self,
        *,
        notes_text: str,
        question: Optional[str],
        indication: str,
    ) -> NotePasteExtractResponse:
        knowledge = self.load_knowledge(indication)
        effective_question = question or _DEFAULT_QUESTIONS.get(
            indication, "Summarize the relevant clinical history in the notes."
        )

        client = OpenAI(api_key=self.api_key or "unused", base_url=self.base_url)
        resp = client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            max_tokens=3000,
            response_format={"type": "json_object"},
            messages=self._prompt(notes_text, effective_question, knowledge),
        )
        choice = resp.choices[0]
        raw = choice.message.content or ""
        if not raw.strip() and getattr(choice, "finish_reason", None) == "length":
            raise NotePasteError(
                "Model response was cut off by the token limit before any "
                "content was produced — try shorter notes, or report this "
                "so the token budget can be raised."
            )
        parsed = self._parse_json(raw)
        return self._verify(parsed, notes_text)
