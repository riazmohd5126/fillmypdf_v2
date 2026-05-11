"""
Profile Repository
==================
Data access layer for profiles
"""

import json
import uuid
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime

from ..config import settings
from ..models import Profile, ProfileCreate, ProfileUpdate
from ..utils.encryption import Encryption


class ProfileRepository:
    """Repository for profile persistence"""
    
    def __init__(self):
        # Read settings live (via properties) so test monkeypatching is honored.
        settings.PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def storage_dir(self) -> Path:
        path = settings.PROFILES_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def encryption_key(self) -> str:
        return settings.PROFILES_ENCRYPTION_KEY

    @property
    def encryption_enabled(self) -> bool:
        return settings.PROFILES_ENCRYPTION_ENABLED

    def _get_file_path(self, profile_id: str) -> Path:
        """Get file path for profile"""
        return self.storage_dir / f"{profile_id}.json"
    
    def _encrypt_data(self, data: Dict[str, str]) -> Dict[str, str]:
        """Encrypt sensitive fields in data"""
        if not self.encryption_enabled:
            return data
        
        encrypted_data = {}
        for key, value in data.items():
            if Encryption.is_sensitive_field(key):
                encrypted_data[f"{key}_encrypted"] = Encryption.encrypt(value, self.encryption_key)
            else:
                encrypted_data[key] = value
        
        return encrypted_data
    
    def _decrypt_data(self, data: Dict[str, str]) -> Dict[str, str]:
        """Decrypt encrypted fields in data"""
        if not self.encryption_enabled:
            return data
        
        decrypted_data = {}
        for key, value in data.items():
            if key.endswith('_encrypted'):
                original_key = key.replace('_encrypted', '')
                try:
                    decrypted_data[original_key] = Encryption.decrypt(value, self.encryption_key)
                except:
                    decrypted_data[original_key] = "[DECRYPTION_ERROR]"
            else:
                decrypted_data[key] = value
        
        return decrypted_data
    
    def save(self, profile: Dict) -> bool:
        """Save profile to storage"""
        try:
            file_path = self._get_file_path(profile['id'])
            with open(file_path, 'w') as f:
                json.dump(profile, f, indent=2, default=str)
            return True
        except Exception as e:
            print(f"Error saving profile: {e}")
            return False
    
    def get(self, profile_id: str) -> Optional[Dict]:
        """Get profile by ID"""
        try:
            file_path = self._get_file_path(profile_id)
            if not file_path.exists():
                return None
            
            with open(file_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading profile: {e}")
            return None
    
    def list_all(self) -> List[Dict]:
        """List all profiles"""
        profiles = []
        for file_path in self.storage_dir.glob("*.json"):
            try:
                with open(file_path, 'r') as f:
                    profile = json.load(f)
                    profiles.append(profile)
            except Exception as e:
                print(f"Skipping corrupt profile file {file_path.name}: {e}")
                continue
        
        return sorted(profiles, key=lambda p: p.get('created_at', ''), reverse=True)
    
    def delete(self, profile_id: str) -> bool:
        """Delete profile"""
        try:
            file_path = self._get_file_path(profile_id)
            if file_path.exists():
                file_path.unlink()
                return True
            return False
        except Exception as e:
            print(f"Error deleting profile: {e}")
            return False
    
    def increment_usage(self, profile_id: str) -> bool:
        """Increment profile usage count"""
        profile = self.get(profile_id)
        if not profile:
            return False
        
        profile['usage_count'] = profile.get('usage_count', 0) + 1
        profile['updated_at'] = datetime.now().isoformat()
        
        return self.save(profile)
