"""Shared demo profiles are created once and visible to any clinic key."""

from __future__ import annotations

import pytest

from fillmypdf.models import ProfileUpdate
from fillmypdf.repositories.profile_repository import ProfileRepository
from fillmypdf.services.profile_seed import (
    DEMO_PROFILES,
    SEED_ORG,
    SEED_OWNER,
    install_demo_profiles,
)
from fillmypdf.services.profile_service import ProfileService


def test_install_creates_five_shared_then_skips():
    first = install_demo_profiles()
    assert first["created"] == 5
    assert first["skipped"] == 0
    second = install_demo_profiles()
    assert second["created"] == 0
    assert second["skipped"] == 5


def test_clinic_can_read_demo_profiles():
    install_demo_profiles()
    svc = ProfileService()
    visible = svc.list_profiles(owner_id="key_unrelated_clinic", tier="pro")
    ids = {p.id for p in visible}
    assert {row["id"] for row in DEMO_PROFILES} <= ids
    patient = next(p for p in visible if p.id == "prof_demo_patient")
    assert patient.shared is True
    assert patient.org_id == SEED_ORG
    assert patient.owner_id == SEED_OWNER


def test_clinic_cannot_edit_demo_profiles():
    install_demo_profiles()
    svc = ProfileService()
    with pytest.raises(ValueError, match="not found"):
        svc.update_profile(
            "prof_demo_patient",
            ProfileUpdate(name="Hacked"),
            owner_id="key_unrelated_clinic",
            tier="pro",
        )


def test_use_profile_decrypts_demo_dob():
    install_demo_profiles()
    svc = ProfileService()
    data = svc.use_profile(
        "prof_demo_patient", owner_id="key_unrelated_clinic", tier="pro"
    )
    assert data.get("patient_first_name") == "Jordan"
    assert data.get("patient_dob") == "1988-04-12"
    repo = ProfileRepository()
    raw = repo.get("prof_demo_patient")
    stored = (raw or {}).get("data", {})
    assert "patient_dob" not in stored
    assert "patient_dob_encrypted" in stored
