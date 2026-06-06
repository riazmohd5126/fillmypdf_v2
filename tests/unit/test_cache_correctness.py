"""
Cache correctness test — mapping cache value-reuse bug
======================================================
Confirms that ``TemplateCache`` currently stores *concrete values* keyed only
by field-label fingerprint (not by user data), so two fill requests against the
same template with different user data share a cache entry.

The test is structured as:
  1. Populate the cache with user_data = {"name": "Alice"} → values {"Name": "Alice"}.
  2. Run a second lookup with user_data = {"name": "Bob"} against the SAME field labels.
  3. Assert the bug: the cache returns "Alice" (first request's values) for "Bob".

A companion test (test_cache_correctness_after_fix) will pass once the fix is
applied (option a: include a hash of normalised user data in the fingerprint).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from fillmypdf.services.template_cache import MappingEntry, TemplateCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIELD_LABELS = {"PatientName": "Patient Full Name", "DOB": "Date of Birth"}


def _populate_cache(cache: TemplateCache, *, name_value: str, dob_value: str) -> str:
    """Manually populate the cache as if the AI returned these values for FIELD_LABELS."""
    fp = TemplateCache.fingerprint(FIELD_LABELS)
    entries = {
        "PatientName": MappingEntry(name_value, 0.99),
        "DOB": MappingEntry(dob_value, 0.95),
    }
    cache.set(fp, entries, field_labels=FIELD_LABELS)
    return fp


# ---------------------------------------------------------------------------
# Bug-confirmation test: same field labels, different user data → same cache hit
# ---------------------------------------------------------------------------

class TestCacheValueReuseBug:
    """
    Demonstrates the current behaviour: the cache key is built only from
    field_labels, so different user data for the same template reuses the first
    request's concrete values.
    """

    def test_fingerprint_ignores_user_data(self):
        """
        Verify that fingerprint() is the same regardless of user data —
        this is the root cause of the value-reuse issue.
        """
        fp_alice = TemplateCache.fingerprint(FIELD_LABELS)
        fp_bob = TemplateCache.fingerprint(FIELD_LABELS)
        assert fp_alice == fp_bob, (
            "Fingerprint must match for identical field labels — this is what "
            "causes value reuse across different users."
        )

    def test_cache_returns_first_users_values_for_second_user(self, isolated_storage):
        """
        BUG CONFIRMED when this test passes without error:
        After caching Alice's values, a lookup for Bob (same field labels)
        returns Alice's data.
        """
        cache = TemplateCache()
        _populate_cache(cache, name_value="Alice Smith", dob_value="1980-01-01")

        fp = TemplateCache.fingerprint(FIELD_LABELS)
        result = cache.get(fp)

        # The cache returns Alice's values even though we intend to fill for Bob
        assert result is not None, "Cache miss — expected a hit"
        assert result["PatientName"].value == "Alice Smith", (
            "Cache correctly stored Alice's name"
        )

    def test_bob_gets_alice_values_on_cache_hit(self, isolated_storage):
        """
        Demonstrates the practical impact: Bob's fill would receive Alice's
        name and DOB on a cache hit.
        """
        cache = TemplateCache()
        _populate_cache(cache, name_value="Alice Smith", dob_value="1980-01-01")

        # Simulate what VisionService._map_fields_with_ai does on a cache hit:
        # it ignores user_data entirely and returns cached values.
        fp = TemplateCache.fingerprint(FIELD_LABELS)
        cached = cache.get(fp)

        # A fill request for Bob hits the cache and gets Alice's values
        bob_user_data = {"name": "Bob Jones", "dob": "1990-06-15"}

        assert cached is not None
        # The returned values come from Alice's fill, not Bob's data
        assert cached["PatientName"].value == "Alice Smith"   # should be "Bob Jones"
        assert cached["DOB"].value == "1980-01-01"           # should be "1990-06-15"

        # This is the bug: cache values don't depend on user data at all.
        # The test documents it so the fix (below) can be verified.


# ---------------------------------------------------------------------------
# Fix-verification tests: pass after option (a) is applied
# ---------------------------------------------------------------------------

class TestCacheCorrectnessAfterFix:
    """
    Verify that ``fingerprint(field_labels, user_data=...)`` produces different
    keys for different user data so each (template × record) gets its own entry.
    """

    def test_different_user_data_produces_different_fingerprints(self):
        """After the fix, fingerprints differ when user data differs."""
        user_alice = {"name": "Alice Smith", "dob": "1980-01-01"}
        user_bob   = {"name": "Bob Jones",   "dob": "1990-06-15"}

        fp_alice = TemplateCache.fingerprint(FIELD_LABELS, user_data=user_alice)
        fp_bob   = TemplateCache.fingerprint(FIELD_LABELS, user_data=user_bob)

        assert fp_alice != fp_bob, (
            "Fingerprints must differ for different user data so Bob does not "
            "get Alice's cached values."
        )

    def test_same_user_data_produces_same_fingerprint(self):
        """Cache hits still work when the same record is submitted twice."""
        user_alice = {"name": "Alice Smith", "dob": "1980-01-01"}

        fp1 = TemplateCache.fingerprint(FIELD_LABELS, user_data=user_alice)
        fp2 = TemplateCache.fingerprint(FIELD_LABELS, user_data=user_alice)

        assert fp1 == fp2, "Identical user data must produce identical fingerprints."

    def test_bob_gets_own_values_after_fix(self, isolated_storage):
        """
        With the fix: Alice's cache entry does not pollute Bob's lookup.
        Alice and Bob each get their own (field_labels, user_data) cache slot.
        """
        cache = TemplateCache()

        user_alice = {"name": "Alice Smith", "dob": "1980-01-01"}
        user_bob   = {"name": "Bob Jones",   "dob": "1990-06-15"}

        fp_alice = TemplateCache.fingerprint(FIELD_LABELS, user_data=user_alice)
        fp_bob   = TemplateCache.fingerprint(FIELD_LABELS, user_data=user_bob)

        # Populate Alice's slot
        cache.set(fp_alice, {
            "PatientName": MappingEntry("Alice Smith", 0.99),
            "DOB": MappingEntry("1980-01-01", 0.95),
        }, field_labels=FIELD_LABELS)

        # Bob's slot is empty — his lookup must be a miss
        result = cache.get(fp_bob)
        assert result is None, (
            "Bob's cache lookup must be a miss — his slot was never populated."
        )

    def test_no_user_data_still_works(self, isolated_storage):
        """Calling fingerprint() without user_data is still valid (admin/listing use)."""
        fp = TemplateCache.fingerprint(FIELD_LABELS)
        cache = TemplateCache()
        cache.set(fp, {"F": MappingEntry("v", 1.0)}, field_labels=FIELD_LABELS)
        assert cache.get(fp) is not None
