"""
Profile Service
===============
Business logic for profile management.

Profiles are scoped to the calling API key (``owner_id``). Legacy profiles
without ``owner_id`` are visible only to admin keys until claimed/recreated.
Shared profiles (``shared=true`` + ``org_id``) are readable by other keys that
present the same ``org_id`` on the profile record (org matching is by value on
the profile itself for MVP — keys do not yet carry an org claim).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, List, Dict

from ..models import Profile, ProfileCreate, ProfileUpdate
from ..repositories.profile_repository import ProfileRepository
from ..config import settings


class ProfileService:
    """Service for profile management"""

    def __init__(self):
        self.repository = ProfileRepository()

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    def _is_admin(self, tier: str) -> bool:
        return (tier or "").lower() == "admin"

    def can_access(
        self,
        profile: Dict,
        *,
        owner_id: Optional[str],
        tier: str = "free",
        for_write: bool = False,
    ) -> bool:
        """Return True if the caller may read (or write) this profile."""
        if self._is_admin(tier):
            return True
        prow = profile.get("owner_id")
        # Legacy (no owner): admin-only
        if not prow:
            return False
        if prow == owner_id:
            return True
        if for_write:
            return False
        # Org-shared library: any authenticated key that knows the org_id
        # can read (MVP). Write stays owner-only.
        if profile.get("shared") and profile.get("org_id"):
            return True
        return False

    def _require_access(
        self,
        profile_id: str,
        *,
        owner_id: Optional[str],
        tier: str = "free",
        for_write: bool = False,
    ) -> Dict:
        profile = self.repository.get(profile_id)
        if not profile:
            raise ValueError("Profile not found")
        if not self.can_access(profile, owner_id=owner_id, tier=tier, for_write=for_write):
            raise ValueError("Profile not found")
        return profile

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_profile(
        self,
        create_data: ProfileCreate,
        tier: str = "free",
        *,
        owner_id: Optional[str] = None,
    ) -> Profile:
        """Create new profile owned by ``owner_id`` (API key id)."""
        existing = self.repository.list_for_owner(owner_id) if owner_id else []
        # Admins also count only their own for the free-tier style limit;
        # unlimited tiers skip the check.
        limit = settings.PROFILE_LIMITS.get(tier, settings.PROFILES_FREE_LIMIT)
        if limit != -1 and len(existing) >= limit:
            raise ValueError(
                f"{tier.title()} tier limit reached ({limit} profile"
                f"{'s' if limit != 1 else ''}). Upgrade to Pro for unlimited profiles."
            )

        profile_id = f"prof_{uuid.uuid4().hex[:12]}"
        encrypted_data = self.repository._encrypt_data(create_data.data)
        now = datetime.now()
        profile_dict = {
            "id": profile_id,
            "name": create_data.name,
            "profile_type": create_data.profile_type,
            "owner_id": owner_id,
            "org_id": (create_data.org_id or "").strip() or None,
            "shared": bool(create_data.shared),
            "data": encrypted_data,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "usage_count": 0,
        }
        if not self.repository.save(profile_dict):
            raise Exception("Failed to save profile")
        return self._to_model(profile_dict)

    def get_profile(
        self,
        profile_id: str,
        *,
        owner_id: Optional[str] = None,
        tier: str = "free",
    ) -> Optional[Profile]:
        try:
            profile = self._require_access(
                profile_id, owner_id=owner_id, tier=tier, for_write=False
            )
        except ValueError:
            return None
        return self._to_model(profile)

    def list_profiles(
        self,
        *,
        owner_id: Optional[str] = None,
        tier: str = "free",
        profile_type: Optional[str] = None,
    ) -> List[Profile]:
        """List profiles visible to this caller."""
        if self._is_admin(tier):
            rows = self.repository.list_all()
        else:
            rows = self.repository.list_visible(owner_id=owner_id)
        if profile_type:
            pt = profile_type.strip().lower()
            rows = [p for p in rows if (p.get("profile_type") or "") == pt]
        return [self._to_model(p) for p in rows]

    def update_profile(
        self,
        profile_id: str,
        update_data: ProfileUpdate,
        *,
        owner_id: Optional[str] = None,
        tier: str = "free",
    ) -> Profile:
        profile_dict = self._require_access(
            profile_id, owner_id=owner_id, tier=tier, for_write=True
        )
        if update_data.name is not None:
            profile_dict["name"] = update_data.name
        if update_data.profile_type is not None:
            profile_dict["profile_type"] = update_data.profile_type
        if update_data.data is not None:
            profile_dict["data"] = self.repository._encrypt_data(update_data.data)
        if update_data.shared is not None:
            profile_dict["shared"] = bool(update_data.shared)
        if update_data.org_id is not None:
            profile_dict["org_id"] = (update_data.org_id or "").strip() or None
        # Claim legacy admin-edited profiles
        if not profile_dict.get("owner_id") and owner_id:
            profile_dict["owner_id"] = owner_id
        profile_dict["updated_at"] = datetime.now().isoformat()
        if not self.repository.save(profile_dict):
            raise Exception("Failed to update profile")
        return self._to_model(profile_dict)

    def delete_profile(
        self,
        profile_id: str,
        *,
        owner_id: Optional[str] = None,
        tier: str = "free",
    ) -> bool:
        self._require_access(
            profile_id, owner_id=owner_id, tier=tier, for_write=True
        )
        return self.repository.delete(profile_id)

    def use_profile(
        self,
        profile_id: str,
        *,
        owner_id: Optional[str] = None,
        tier: str = "free",
    ) -> Dict[str, str]:
        """Get decrypted profile data for form filling. Increments usage."""
        profile_dict = self._require_access(
            profile_id, owner_id=owner_id, tier=tier, for_write=False
        )
        decrypted_data = self.repository._decrypt_data(profile_dict.get("data", {}))
        self.repository.increment_usage(profile_id)
        return decrypted_data

    def use_profiles(
        self,
        profile_ids: List[str],
        *,
        owner_id: Optional[str] = None,
        tier: str = "free",
    ) -> Dict[str, str]:
        """Merge multiple accessible profiles into one flat dict."""
        merged: Dict[str, str] = {}
        for pid in profile_ids:
            try:
                profile_dict = self._require_access(
                    pid, owner_id=owner_id, tier=tier, for_write=False
                )
            except ValueError:
                continue
            ptype = profile_dict.get("profile_type", "")
            data = self.repository._decrypt_data(profile_dict.get("data", {}))
            self.repository.increment_usage(pid)
            merged.update(data)
            if ptype:
                for k, v in data.items():
                    merged[f"{ptype}_{k}"] = v
        return merged

    def _to_model(self, profile_dict: Dict) -> Profile:
        data_preview = {}
        for key, value in profile_dict.get("data", {}).items():
            if not key.endswith("_encrypted"):
                data_preview[key] = value
        return Profile(
            id=profile_dict["id"],
            name=profile_dict["name"],
            profile_type=profile_dict["profile_type"],
            owner_id=profile_dict.get("owner_id"),
            org_id=profile_dict.get("org_id"),
            shared=bool(profile_dict.get("shared", False)),
            created_at=datetime.fromisoformat(profile_dict["created_at"]),
            updated_at=datetime.fromisoformat(profile_dict["updated_at"]),
            usage_count=profile_dict.get("usage_count", 0),
            data_preview=data_preview,
        )
