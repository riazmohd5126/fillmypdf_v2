"""
API Key Routes
==============
Admin-only CRUD for API keys.

Note: every endpoint here requires an admin-tier API key.
Users self-managing their own keys is a future feature; for now keys are
issued centrally by the operator.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from ...models import APIKey, APIKeyCreate, APIKeyCreateResponse
from ...services.api_key_service import APIKeyService
from ..dependencies.auth import require_admin

router = APIRouter(
    prefix="/keys",
    tags=["api-keys"],
    dependencies=[Depends(require_admin)],
)

service = APIKeyService()


@router.post("/", response_model=APIKeyCreateResponse, status_code=201)
async def create_key(payload: APIKeyCreate):
    """
    Create a new API key.

    The plaintext key is returned **once** in this response. Save it now —
    only the bcrypt hash is stored, so the plaintext can never be recovered.
    """
    try:
        return service.create_key(payload)
    except Exception as e:
        raise HTTPException(500, f"Failed to create key: {e}")


@router.get("/", response_model=List[APIKey])
async def list_keys():
    """List all API keys (metadata only — no plaintext or hash)."""
    return service.list_keys()


@router.get("/{key_id}", response_model=APIKey)
async def get_key(key_id: str):
    key = service.get_key(key_id)
    if not key:
        raise HTTPException(404, "API key not found")
    return key


@router.post("/{key_id}/revoke", response_model=APIKey)
async def revoke_key(key_id: str):
    """Revoke a key (it stays in storage but can no longer authenticate)."""
    if not service.revoke_key(key_id):
        raise HTTPException(404, "API key not found")
    return service.get_key(key_id)


@router.delete("/{key_id}", status_code=204)
async def delete_key(key_id: str):
    """Permanently delete a key from storage."""
    if not service.delete_key(key_id):
        raise HTTPException(404, "API key not found")
    return None
