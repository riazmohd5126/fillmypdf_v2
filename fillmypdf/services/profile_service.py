"""
Profile Service
===============
Business logic for profile management
"""

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
    
    def create_profile(self, create_data: ProfileCreate, tier: str = "free") -> Profile:
        """Create new profile.

        Args:
            create_data: Profile data.
            tier: Subscription tier of the caller (free/pro/business/admin).
                  Used to enforce per-tier profile count limits.
        """
        # Check tier limits (-1 means unlimited)
        existing_profiles = self.repository.list_all()
        limit = settings.PROFILE_LIMITS.get(tier, settings.PROFILES_FREE_LIMIT)
        if limit != -1 and len(existing_profiles) >= limit:
            raise ValueError(
                f"{tier.title()} tier limit reached ({limit} profile{'s' if limit != 1 else ''}). "
                f"Upgrade to Pro for unlimited profiles."
            )
        
        # Generate ID
        profile_id = f"prof_{uuid.uuid4().hex[:12]}"
        
        # Encrypt sensitive data
        encrypted_data = self.repository._encrypt_data(create_data.data)
        
        # Create profile dict
        now = datetime.now()
        profile_dict = {
            'id': profile_id,
            'name': create_data.name,
            'profile_type': create_data.profile_type,
            'data': encrypted_data,
            'created_at': now.isoformat(),
            'updated_at': now.isoformat(),
            'usage_count': 0,
        }
        
        # Save
        if not self.repository.save(profile_dict):
            raise Exception("Failed to save profile")
        
        # Return as model (with preview, no encrypted data)
        return self._to_model(profile_dict)
    
    def get_profile(self, profile_id: str) -> Optional[Profile]:
        """Get profile by ID"""
        profile_dict = self.repository.get(profile_id)
        if not profile_dict:
            return None
        
        return self._to_model(profile_dict)
    
    def list_profiles(self) -> List[Profile]:
        """List all profiles"""
        profiles = self.repository.list_all()
        return [self._to_model(p) for p in profiles]
    
    def update_profile(self, profile_id: str, update_data: ProfileUpdate) -> Profile:
        """Update profile"""
        profile_dict = self.repository.get(profile_id)
        if not profile_dict:
            raise ValueError("Profile not found")
        
        # Update fields
        if update_data.name is not None:
            profile_dict['name'] = update_data.name
        
        if update_data.profile_type is not None:
            profile_dict['profile_type'] = update_data.profile_type
        
        if update_data.data is not None:
            # Encrypt new data
            encrypted_data = self.repository._encrypt_data(update_data.data)
            profile_dict['data'] = encrypted_data
        
        profile_dict['updated_at'] = datetime.now().isoformat()
        
        # Save
        if not self.repository.save(profile_dict):
            raise Exception("Failed to update profile")
        
        return self._to_model(profile_dict)
    
    def delete_profile(self, profile_id: str) -> bool:
        """Delete profile"""
        return self.repository.delete(profile_id)
    
    def use_profile(self, profile_id: str) -> Dict[str, str]:
        """
        Get decrypted profile data for use in form filling
        Also increments usage count
        """
        profile_dict = self.repository.get(profile_id)
        if not profile_dict:
            raise ValueError(f"Profile not found: {profile_id}")
        
        # Decrypt data
        decrypted_data = self.repository._decrypt_data(profile_dict.get('data', {}))
        
        # Increment usage
        self.repository.increment_usage(profile_id)
        
        return decrypted_data
    
    def _to_model(self, profile_dict: Dict) -> Profile:
        """Convert dict to Profile model"""
        # Create preview (non-encrypted fields only)
        data_preview = {}
        for key, value in profile_dict.get('data', {}).items():
            if not key.endswith('_encrypted'):
                data_preview[key] = value
        
        return Profile(
            id=profile_dict['id'],
            name=profile_dict['name'],
            profile_type=profile_dict['profile_type'],
            created_at=datetime.fromisoformat(profile_dict['created_at']),
            updated_at=datetime.fromisoformat(profile_dict['updated_at']),
            usage_count=profile_dict.get('usage_count', 0),
            data_preview=data_preview,
        )
