"""Trial seed packs copy onto an empty storage dir without clobbering maps."""

from __future__ import annotations

import json
from pathlib import Path

from fillmypdf.services.trial_seed import SEED_DIR, install_trial_seed, seed_catalog


def test_seed_catalog_has_five_locked_packs():
    catalog = seed_catalog()
    assert len(catalog) == 5
    ids = {row["id"] for row in catalog}
    assert "aetna_az-prescription-drug-prior-authorizathion-request-_263213f9cd4c" in ids
    for row in catalog:
        assert (SEED_DIR / "templates" / row["id"] / "template.pdf").is_file()
        assert (SEED_DIR / "maps" / f"{row['fingerprint']}.json").is_file()
        data = json.loads((SEED_DIR / "maps" / f"{row['fingerprint']}.json").read_text())
        assert data["reviewed"] is True
        assert data["template_id"] == row["id"]


def test_install_copies_missing_then_skips_maps(tmp_path: Path):
    first = install_trial_seed(tmp_path)
    assert first["templates"] == 5
    assert first["maps"] == 5
    assert first["manifests"] == 5
    second = install_trial_seed(tmp_path)
    assert second["templates"] == 0
    assert second["maps"] == 0
    assert second["skipped"] == 5
    dest = tmp_path / "templates" / seed_catalog()[0]["id"] / "template.pdf"
    assert dest.is_file()
