"""
Profile API Routes
==================
CRUD endpoints for user profiles
"""

from typing import Callable, List

from fastapi import APIRouter, Depends, HTTPException

from ...models import Profile, ProfileCreate, ProfileUpdate
from ...services.profile_service import ProfileService
from ..dependencies.auth import require_api_key

router = APIRouter(
    prefix="/profiles",
    tags=["profiles"],
    dependencies=[Depends(require_api_key)],
)

profile_service = ProfileService()

# Injected by main.py after app startup to avoid circular imports
increment_profiles_created: Callable[[], None] = lambda: None


@router.post("/", response_model=Profile, status_code=201)
async def create_profile(
    profile_data: ProfileCreate,
    api_key: dict = Depends(require_api_key),
):
    """
    Create a new profile

    **Saves personal or business data for reuse across forms**

    - **name**: Profile name (e.g., "My Company", "Personal Info")
    - **profile_type**: personal, business, spouse, dependent, or custom
    - **data**: Key-value pairs (SSN, DOB, etc. are encrypted automatically)

    Per-tier limits apply:
      - free: 1 profile
      - pro / business / admin: unlimited
    """
    try:
        result = profile_service.create_profile(profile_data, tier=api_key.get("tier", "free"))
        increment_profiles_created()
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create profile: {e}")


@router.get("/", response_model=List[Profile])
async def list_profiles():
    """
    List all profiles

    Returns all saved profiles (encrypted data not included in response)
    """
    return profile_service.list_profiles()


@router.get("/{profile_id}", response_model=Profile)
async def get_profile(profile_id: str):
    """Get profile by ID"""
    profile = profile_service.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.patch("/{profile_id}", response_model=Profile)
async def update_profile(profile_id: str, update_data: ProfileUpdate):
    """
    Update profile

    Only provided fields will be updated
    """
    try:
        return profile_service.update_profile(profile_id, update_data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update profile: {e}")


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: str):
    """Delete profile"""
    if not profile_service.delete_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return None
