"""
Unit tests for fillmypdf.utils.encryption.Encryption
====================================================
AES-256-GCM round-trip + tamper detection + sensitive-field detection.
"""

import pytest

from fillmypdf.utils.encryption import Encryption


class TestEncryptDecryptRoundTrip:
    KEY = "test-key-please-change-32-chars-aaaa"

    def test_round_trip_simple_string(self):
        ct = Encryption.encrypt("hello world", self.KEY)
        assert ct != "hello world"
        assert Encryption.decrypt(ct, self.KEY) == "hello world"

    def test_round_trip_unicode(self):
        msg = "patient: María González — DOB 1985-07-22 ☆"
        ct = Encryption.encrypt(msg, self.KEY)
        assert Encryption.decrypt(ct, self.KEY) == msg

    def test_round_trip_long_string(self):
        msg = "x" * 10_000
        ct = Encryption.encrypt(msg, self.KEY)
        assert Encryption.decrypt(ct, self.KEY) == msg

    def test_empty_string_returns_empty(self):
        assert Encryption.encrypt("", self.KEY) == ""
        assert Encryption.decrypt("", self.KEY) == ""


class TestSecurityProperties:
    KEY = "test-key-please-change-32-chars-aaaa"

    def test_same_plaintext_different_ciphertexts(self):
        """AES-GCM uses a random nonce — ciphertexts must differ."""
        ct1 = Encryption.encrypt("same message", self.KEY)
        ct2 = Encryption.encrypt("same message", self.KEY)
        assert ct1 != ct2

    def test_decrypt_with_wrong_key_fails(self):
        ct = Encryption.encrypt("secret data", self.KEY)
        with pytest.raises(ValueError, match="Decryption failed"):
            Encryption.decrypt(ct, "wrong-key-aaaaaaaaaaaaaaaaaaaaaaaaaa")

    def test_tampered_ciphertext_fails(self):
        """GCM auth tag should detect tampering."""
        import base64
        ct = Encryption.encrypt("untampered message", self.KEY)
        # Flip a byte in the middle of the ciphertext
        raw = bytearray(base64.b64decode(ct))
        raw[40] = (raw[40] + 1) % 256
        tampered = base64.b64encode(bytes(raw)).decode()
        with pytest.raises(ValueError, match="Decryption failed"):
            Encryption.decrypt(tampered, self.KEY)

    def test_truncated_ciphertext_fails(self):
        ct = Encryption.encrypt("untouched", self.KEY)
        with pytest.raises(ValueError, match="Decryption failed"):
            Encryption.decrypt(ct[:20], self.KEY)


class TestSensitiveFieldDetection:
    @pytest.mark.parametrize("field", [
        "ssn", "SSN", "social_security_number", "Social", "security",
        "ein", "ITIN", "tax_id", "taxpayer_id",
        "account_number", "routing_number",
        "card_number", "credit_card", "debit_card",
        "password", "pin",
        "dob", "date_of_birth", "birthdate",
        "drivers_license", "passport_number",
    ])
    def test_sensitive_fields_detected(self, field):
        assert Encryption.is_sensitive_field(field) is True

    @pytest.mark.parametrize("field", [
        "first_name", "last_name", "email", "phone",
        "address", "city", "state", "zip",
        "company", "employer", "department",
    ])
    def test_non_sensitive_fields_not_flagged(self, field):
        assert Encryption.is_sensitive_field(field) is False
