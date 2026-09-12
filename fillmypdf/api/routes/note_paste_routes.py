"""
Note Paste API
===============
POST /api/v1/note-paste/extract — nurse pastes visit notes, gets back a
drafted clinical-justification answer with verbatim-verified evidence
quotes. See services/note_paste_service.py for the extraction + the
deterministic verification layer.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from ...config import settings
from ...models.note_paste import NotePasteExtractRequest, NotePasteExtractResponse
from ...services.ai_provider import assert_egress_allowed, resolve_ai_config
from ...services.note_paste_service import NotePasteError, NotePasteService
from ..dependencies.auth import require_api_key

router = APIRouter(
    prefix="/note-paste",
    tags=["note-paste"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/indications", summary="List available knowledge-file indications")
async def list_indications():
    from pathlib import Path

    knowledge_dir = Path(__file__).resolve().parent.parent.parent / "knowledge"
    return {
        "indications": sorted(p.stem for p in knowledge_dir.glob("*.md"))
        if knowledge_dir.exists()
        else []
    }


@router.post(
    "/extract",
    response_model=NotePasteExtractResponse,
    summary="Extract a clinical-justification answer from pasted visit notes",
)
async def extract_from_notes(
    body: NotePasteExtractRequest,
    api_key: dict = Depends(require_api_key),
):
    if not body.notes_text.strip():
        raise HTTPException(400, "notes_text is empty")

    resolved_key = (
        (body.ai_api_key or "").strip()
        or (settings.GEMINI_API_KEY or "").strip()
        or os.getenv("GEMINI_API_KEY", "")
    )
    ai_key, ai_base_url, ai_model = resolve_ai_config(
        request_api_key=resolved_key,
        request_base_url=body.ai_base_url,
        request_model=body.ai_model,
        provider_hint=body.ai_provider,
    )

    if body.ai_provider != "local" and settings.AI_PROVIDER != "local" and not ai_key:
        raise HTTPException(
            400,
            "A Gemini API key is required (pass ai_api_key=, set GEMINI_API_KEY, "
            "or switch ai_provider='local' for an on-prem model).",
        )

    try:
        assert_egress_allowed(ai_base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    service = NotePasteService(api_key=ai_key, base_url=ai_base_url, model=ai_model)
    try:
        return service.extract(
            notes_text=body.notes_text,
            question=body.question,
            indication=body.indication,
        )
    except NotePasteError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # model / network failure
        raise HTTPException(502, f"Note extraction failed: {exc}")
