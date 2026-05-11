"""
Encryption Utility
==================
AES-256-GCM encryption for sensitive profile data
"""

import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend


class Encryption:
    """AES-256-GCM encryption/decryption"""
    
    ITERATIONS = 100_000
    KEY_SIZE = 32  # 256 bits
    
    @staticmethod
    def _derive_key(password: str, salt: bytes) -> bytes:
        """Derive encryption key from password using PBKDF2"""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=Encryption.KEY_SIZE,
            salt=salt,
            iterations=Encryption.ITERATIONS,
            backend=default_backend()
        )
        return kdf.derive(password.encode())
    
    @staticmethod
    def encrypt(plaintext: str, password: str) -> str:
        """
        Encrypt plaintext using AES-256-GCM
        
        Returns base64(salt + nonce + ciphertext + tag)
        """
        if not plaintext:
            return ""
        
        # Generate random salt and nonce
        salt = os.urandom(16)
        nonce = os.urandom(12)
        
        # Derive key
        key = Encryption._derive_key(password, salt)
        
        # Encrypt
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
        
        # Combine: salt + nonce + ciphertext (ciphertext includes auth tag)
        combined = salt + nonce + ciphertext
        
        # Return as base64
        return base64.b64encode(combined).decode()
    
    @staticmethod
    def decrypt(ciphertext_b64: str, password: str) -> str:
        """
        Decrypt AES-256-GCM ciphertext
        
        Expects base64(salt + nonce + ciphertext + tag)
        """
        if not ciphertext_b64:
            return ""
        
        try:
            # Decode base64
            combined = base64.b64decode(ciphertext_b64)
            
            # Extract components
            salt = combined[:16]
            nonce = combined[16:28]
            ciphertext = combined[28:]
            
            # Derive key
            key = Encryption._derive_key(password, salt)
            
            # Decrypt
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            
            return plaintext.decode()
        
        except Exception as e:
            raise ValueError(f"Decryption failed (bad key or corrupted data): {e}") from e
    
    @staticmethod
    def is_sensitive_field(field_name: str) -> bool:
        """Check if field should be encrypted"""
        sensitive_keywords = [
            'ssn', 'social', 'security',
            'tax', 'ein', 'itin',
            'account', 'routing',
            'card', 'credit', 'debit',
            'password', 'pin',
            'dob', 'birth',
            'license', 'passport',
        ]
        
        field_lower = field_name.lower()
        return any(keyword in field_lower for keyword in sensitive_keywords)
