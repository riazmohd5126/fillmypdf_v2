"""
Shared pytest fixtures.

The most important thing this file does is **redirect all storage paths to
a per-test temporary directory** before any application module is imported,
so tests never touch the real ./storage/ folder.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# Storage isolation — runs before any fillmypdf.* module is imported
# ---------------------------------------------------------------------------

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="fmp_test_"))

os.environ.setdefault("STORAGE_DIR", str(_SESSION_TMP))
os.environ.setdefault("PROFILES_DIR", str(_SESSION_TMP / "profiles"))
os.environ.setdefault("UPLOAD_DIR", str(_SESSION_TMP / "temp" / "uploads"))
os.environ.setdefault("OUTPUT_DIR", str(_SESSION_TMP / "temp" / "outputs"))
os.environ.setdefault("PROFILES_ENCRYPTION_KEY", "test-key-32-bytes-long-aaaaaaaaaa")
os.environ.setdefault("DEBUG", "True")

# Use the lowest bcrypt cost factor in tests — a cost of 12 makes the suite
# crawl (every API-key verify is ~250ms). 4 is bcrypt's minimum (~5ms).
os.environ["BCRYPT_ROUNDS"] = "4"

# Ensure project root is on sys.path so `import fillmypdf` works
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Per-test isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch) -> Iterator[Path]:
    """
    Redirect every storage path to a fresh tmp_path for this test.
    Also reload settings so the new paths take effect.
    """
    profiles_dir = tmp_path / "profiles"
    api_keys_dir = tmp_path / "api_keys"
    upload_dir = tmp_path / "temp" / "uploads"
    output_dir = tmp_path / "temp" / "outputs"
    for d in (profiles_dir, api_keys_dir, upload_dir, output_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Override settings paths in the live module
    from fillmypdf import config as cfg
    monkeypatch.setattr(cfg.settings, "STORAGE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(cfg.settings, "PROFILES_DIR", profiles_dir, raising=True)
    monkeypatch.setattr(cfg.settings, "UPLOAD_DIR", upload_dir, raising=True)
    monkeypatch.setattr(cfg.settings, "OUTPUT_DIR", output_dir, raising=True)

    yield tmp_path


# ---------------------------------------------------------------------------
# API key fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def api_key_service():
    """Fresh APIKeyService bound to the isolated storage."""
    from fillmypdf.services.api_key_service import APIKeyService
    return APIKeyService()


@pytest.fixture
def free_api_key(api_key_service) -> dict:
    """A free-tier API key. Returns dict with 'plain' and 'record'."""
    from fillmypdf.models import APIKeyCreate
    resp = api_key_service.create_key(APIKeyCreate(name="Test Free", tier="free"))
    return {"plain": resp.key, "id": resp.id, "tier": "free"}


@pytest.fixture
def pro_api_key(api_key_service) -> dict:
    from fillmypdf.models import APIKeyCreate
    resp = api_key_service.create_key(APIKeyCreate(name="Test Pro", tier="pro"))
    return {"plain": resp.key, "id": resp.id, "tier": "pro"}


@pytest.fixture
def admin_api_key(api_key_service) -> dict:
    from fillmypdf.models import APIKeyCreate
    resp = api_key_service.create_key(APIKeyCreate(name="Test Admin", tier="admin"))
    return {"plain": resp.key, "id": resp.id, "tier": "admin"}


# ---------------------------------------------------------------------------
# FastAPI TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """FastAPI TestClient bound to the app."""
    from fastapi.testclient import TestClient
    from fillmypdf.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers_free(free_api_key):
    return {"X-API-Key": free_api_key["plain"]}


@pytest.fixture
def auth_headers_pro(pro_api_key):
    return {"X-API-Key": pro_api_key["plain"]}


@pytest.fixture
def auth_headers_admin(admin_api_key):
    return {"X-API-Key": admin_api_key["plain"]}
