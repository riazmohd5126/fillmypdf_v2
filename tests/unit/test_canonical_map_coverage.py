"""PHI-free public_coverage rows match Mapping Review detail filters."""

from fillmypdf.services.canonical_map_cache import CanonicalMapCache


def test_public_coverage_skips_choice_and_splits_status():
    cov = CanonicalMapCache().public_coverage(
        {
            "signature": "",
            "field_labels": {
                "name": "Full name",
                "npi": "Prescriber NPI",
                "skip_me": "Office notes",
                "tick": "Has NPI?",
            },
            "mappings": {
                "name": {"canonical": "patient.full_name"},
                "npi": {},
                "skip_me": {"canonical": "other"},
                "tick": {"canonical": "patient.npi"},
            },
            "field_kinds": {
                "name": "data",
                "npi": "data",
                "skip_me": "data",
                "tick": "choice",
            },
        }
    )
    assert cov["mapped_count"] == 1
    assert cov["unmapped_count"] == 1
    assert cov["other_count"] == 1
    assert cov["total_fields"] == 3
    assert cov["coverage_pct"] == 33.3
    by_field = {row["field"]: row for row in cov["rows"]}
    assert "tick" not in by_field
    assert by_field["name"]["status"] == "mapped"
    assert by_field["name"]["canonical"] == "patient.full_name"
    assert by_field["npi"]["status"] == "unmapped"
    assert by_field["skip_me"]["status"] == "other"
    assert by_field["skip_me"]["canonical"] is None
