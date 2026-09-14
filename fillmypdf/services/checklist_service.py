"""
Checklist Service
==================
Drafts a plain-text "what to submit alongside this PA form" checklist from a
form's own extracted checkbox/question fields (:class:`~fillmypdf.models.
form_spec.FormSpec`). PHI-free: this only ever sees blank-form question text
and option labels — never a patient's filled-in answers — same guarantee as
the canonical-map AI-suggest step it sits next to in Mapping Review.

Grounded strictly in the form's own fields by design: the prompt is built
entirely from ``spec.questions`` / ``spec.long_text``, and explicitly forbids
inventing generic PA-submission staples the form itself doesn't ask about.
An admin reviews and edits the draft before it is ever shown to a clinic
user (see mapping_review_routes.py's checklist endpoints and the lock-time
sync onto TemplateManifest.checklist).
"""

from __future__ import annotations

import json
from typing import List

from openai import OpenAI

from ..models.form_spec import FormSpec

_MAX_ITEMS = 20


class ChecklistError(Exception):
    pass


class ChecklistService:
    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------
    @staticmethod
    def _form_context(spec: FormSpec) -> str:
        """Render the form's own questions/options/narratives as plain text —
        the only material the model is allowed to draw the checklist from."""
        lines: list[str] = []
        for q in spec.questions:
            opts = ", ".join(o.label for o in (q.options or []) if o.label)
            lines.append(f"- Question: {q.question}" + (f" (options: {opts})" if opts else ""))
        for lt in spec.long_text:
            lines.append(f"- Narrative field: {lt.label}")
        return "\n".join(lines)

    def _prompt(self, spec: FormSpec) -> list[dict]:
        context = self._form_context(spec)
        system = (
            "You are drafting a submission checklist for a prior-authorization "
            "form — the supporting documentation a clinic should gather before "
            "faxing or submitting this specific form. You are shown only this "
            "form's own printed checkboxes, questions and narrative fields "
            "below. Two rules matter more than anything else:\n"
            "1. Every checklist item must be directly implied by one of the "
            "questions/fields listed below — e.g. a question asking whether "
            "a prior therapy was tried implies \"chart notes documenting the "
            "prior therapy trial.\" Never invent a generic PA-submission item "
            "(insurance card, cover letter, etc.) that isn't tied to a "
            "specific field shown to you.\n"
            "2. Each item is a short, plain, actionable line — what to gather "
            "or attach, not a restatement of the form's question wording."
        )
        user = (
            f"THIS FORM'S QUESTIONS AND FIELDS:\n{context or '(none extracted yet)'}\n\n"
            "Respond with STRICT JSON only, no markdown fences:\n"
            '{"checklist": ["short actionable item", "..."]}\n'
            f"At most {_MAX_ITEMS} items. Empty list if nothing in the fields "
            "above implies a submission requirement."
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
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except Exception:
                pass
        snippet = (raw or "").strip().replace("\n", " ")[:300]
        raise ChecklistError(
            "Model did not return parseable JSON. "
            f"Raw response started with: {snippet!r}"
            if snippet
            else "Model returned an empty response."
        )

    @staticmethod
    def _normalize_items(raw_items) -> List[str]:
        if not isinstance(raw_items, list):
            return []
        seen: set[str] = set()
        out: List[str] = []
        for item in raw_items:
            text = str(item or "").strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= _MAX_ITEMS:
                break
        return out

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def draft(self, spec: FormSpec) -> List[str]:
        client = OpenAI(api_key=self.api_key or "unused", base_url=self.base_url)
        resp = client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            max_tokens=1500,
            response_format={"type": "json_object"},
            messages=self._prompt(spec),
        )
        choice = resp.choices[0]
        raw = choice.message.content or ""
        if not raw.strip() and getattr(choice, "finish_reason", None) == "length":
            raise ChecklistError(
                "Model response was cut off by the token limit before any "
                "content was produced — try again."
            )
        parsed = self._parse_json(raw)
        return self._normalize_items(parsed.get("checklist"))
