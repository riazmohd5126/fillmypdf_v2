"""
Unit tests for fillmypdf.services.template_cache
=================================================
TemplateCache: fingerprint stability, read/write/TTL/invalidate.
MappingEntry: round-trip serialisation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from fillmypdf.services.template_cache import MappingEntry, TemplateCache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cache(isolated_storage) -> TemplateCache:
    """Fresh cache bound to the per-test tmp dir via isolated_storage."""
    return TemplateCache()


@pytest.fixture
def labels_a():
    return {"FirstName": "Patient Name", "DOB": "Date of Birth", "ICD": "Diagnosis"}


@pytest.fixture
def labels_b():
    return {"FirstName": "Member Name", "DOB": "Date of Birth"}


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

class TestFingerprint:
    def test_same_labels_same_fp(self, labels_a):
        fp1 = TemplateCache.fingerprint(labels_a)
        fp2 = TemplateCache.fingerprint(dict(labels_a))
        assert fp1 == fp2

    def test_insertion_order_does_not_matter(self, labels_a):
        shuffled = dict(reversed(list(labels_a.items())))
        assert TemplateCache.fingerprint(labels_a) == TemplateCache.fingerprint(shuffled)

    def test_different_labels_different_fp(self, labels_a, labels_b):
        assert TemplateCache.fingerprint(labels_a) != TemplateCache.fingerprint(labels_b)

    def test_fp_is_32_hex_chars(self, labels_a):
        fp = TemplateCache.fingerprint(labels_a)
        assert len(fp) == 32
        assert all(c in "0123456789abcdef" for c in fp)


# ---------------------------------------------------------------------------
# MappingEntry serialisation
# ---------------------------------------------------------------------------

class TestMappingEntry:
    def test_round_trip(self):
        e = MappingEntry(value="John Smith", confidence=0.92, source="ai")
        d = e.to_dict()
        e2 = MappingEntry.from_dict(d)
        assert e2.value == "John Smith"
        assert abs(e2.confidence - 0.92) < 1e-6
        assert e2.source == "ai"

    def test_defaults(self):
        e = MappingEntry.from_dict({"value": "X"})
        assert e.confidence == 1.0
        assert e.source == "ai"


# ---------------------------------------------------------------------------
# Cache read / write
# ---------------------------------------------------------------------------

class TestCacheReadWrite:
    def test_miss_returns_none(self, cache, labels_a):
        fp = TemplateCache.fingerprint(labels_a)
        assert cache.get(fp) is None

    def test_set_then_get(self, cache, labels_a):
        fp = TemplateCache.fingerprint(labels_a)
        entries = {
            "FirstName": MappingEntry("Jane", 0.99),
            "DOB": MappingEntry("1980-01-01", 0.95),
        }
        cache.set(fp, entries, field_labels=labels_a)
        result = cache.get(fp)
        assert result is not None
        assert result["FirstName"].value == "Jane"
        assert abs(result["DOB"].confidence - 0.95) < 1e-6

    def test_cache_file_written_to_disk(self, cache, labels_a):
        fp = TemplateCache.fingerprint(labels_a)
        cache.set(fp, {"F1": MappingEntry("x", 1.0)})
        assert (cache.cache_dir / f"{fp}.json").exists()

    def test_version_mismatch_returns_none(self, cache, labels_a):
        fp = TemplateCache.fingerprint(labels_a)
        cache.set(fp, {"F": MappingEntry("v", 1.0)})
        path = cache.cache_dir / f"{fp}.json"
        data = json.loads(path.read_text())
        data["version"] = 999          # corrupt the version
        path.write_text(json.dumps(data))
        assert cache.get(fp) is None

    def test_corrupt_file_returns_none(self, cache, labels_a):
        fp = TemplateCache.fingerprint(labels_a)
        (cache.cache_dir / f"{fp}.json").write_text("not-json")
        assert cache.get(fp) is None


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------

class TestInvalidate:
    def test_invalidate_existing(self, cache, labels_a):
        fp = TemplateCache.fingerprint(labels_a)
        cache.set(fp, {"F": MappingEntry("v", 1.0)})
        assert cache.invalidate(fp) is True
        assert cache.get(fp) is None

    def test_invalidate_missing_returns_false(self, cache):
        assert cache.invalidate("does_not_exist" * 2) is False


# ---------------------------------------------------------------------------
# List entries
# ---------------------------------------------------------------------------

class TestListEntries:
    def test_empty_cache(self, cache):
        assert cache.list_entries() == []

    def test_lists_all_entries(self, cache, labels_a, labels_b):
        fp_a = TemplateCache.fingerprint(labels_a)
        fp_b = TemplateCache.fingerprint(labels_b)
        cache.set(fp_a, {"F": MappingEntry("a", 1.0)}, field_labels=labels_a)
        cache.set(fp_b, {"G": MappingEntry("b", 0.8)}, field_labels=labels_b)
        entries = cache.list_entries()
        assert len(entries) == 2
        fps = {e["fingerprint"] for e in entries}
        assert fp_a in fps and fp_b in fps


# ---------------------------------------------------------------------------
# Disabled cache
# ---------------------------------------------------------------------------

class TestCacheDisabled:
    def test_get_returns_none_when_disabled(self, cache, labels_a, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "TEMPLATE_CACHE_ENABLED", False)
        fp = TemplateCache.fingerprint(labels_a)
        cache.set(fp, {"F": MappingEntry("v", 1.0)})
        assert cache.get(fp) is None

    def test_set_does_nothing_when_disabled(self, cache, labels_a, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "TEMPLATE_CACHE_ENABLED", False)
        fp = TemplateCache.fingerprint(labels_a)
        cache.set(fp, {"F": MappingEntry("v", 1.0)})
        assert not (cache.cache_dir / f"{fp}.json").exists()
