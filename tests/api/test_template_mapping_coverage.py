"""Clinic-visible mapped vs unresolved coverage on /templates (mini app)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fillmypdf.api.routes.templates import router
from fillmypdf.models.template import TemplateManifest
from fillmypdf.services.canonical_map_cache import CanonicalMapCache
from fillmypdf.services.template_service import TemplateService


@pytest.fixture
def tpl_client():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as c:
        yield c


def test_readiness_includes_mapping_coverage(tpl_client, admin_api_key, isolated_storage):
    tid = "priv_clinic_cov_ready"
    TemplateService().add(
        TemplateManifest(id=tid, name="Coverage Ready Form"),
        b"%PDF-1.4\n%%EOF\n",
    )
    CanonicalMapCache().save_full(
        "fp_cov_ready",
        {
            "fingerprint": "fp_cov_ready",
            "signature": "sig_cov",
            "form_label": "cov.pdf",
            "template_id": tid,
            "reviewed": True,
            "mappings": {
                "a": {"canonical": "patient.full_name"},
                "b": {},
            },
            "field_labels": {"a": "Patient name", "b": "Something else"},
        },
    )
    r = tpl_client.get(
        "/api/v1/templates/readiness",
        headers={"X-API-Key": admin_api_key["plain"]},
    )
    assert r.status_code == 200, r.text
    hit = next(x for x in r.json()["items"] if x["template_id"] == tid)
    assert hit["mapped_count"] == 1
    assert hit["unmapped_count"] == 1
    assert hit["total_fields"] == 2
    assert hit["coverage_pct"] == 50.0
    assert hit["added_at"]


def test_mapping_coverage_splits_mapped_and_unresolved(
    tpl_client, free_api_key, isolated_storage
):
    tid = "shared_cov_fields"
    TemplateService().add(
        TemplateManifest(id=tid, name="Coverage Fields Form"),
        b"%PDF-1.4\n%%EOF\n",
    )
    CanonicalMapCache().save_full(
        "fp_cov_fields",
        {
            "fingerprint": "fp_cov_fields",
            "signature": "sig_cov_fields",
            "form_label": tid,
            "template_id": tid,
            "reviewed": True,
            "mappings": {
                "name": {"canonical": "patient.full_name", "value": "should-not-leak"},
                "npi": {},
                "skip_me": {"canonical": "other"},
                "tick": {"canonical": "patient.npi"},
            },
            "field_labels": {
                "name": "Full name",
                "npi": "Prescriber NPI",
                "skip_me": "Office notes",
                "tick": "Has NPI?",
            },
            "field_kinds": {
                "name": "data",
                "npi": "data",
                "skip_me": "data",
                "tick": "choice",
            },
        },
    )
    r = tpl_client.get(
        f"/api/v1/templates/{tid}/mapping-coverage",
        headers={"X-API-Key": free_api_key["plain"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_mapping"] is True
    assert body["reviewed"] is True
    assert body["mapped_count"] == 1
    assert body["unmapped_count"] == 1
    assert body["other_count"] == 1
    assert body["total_fields"] == 3
    assert "catalog" not in body
    statuses = {row["field"]: row for row in body["rows"]}
    assert "tick" not in statuses
    assert statuses["name"]["status"] == "mapped"
    assert statuses["name"]["canonical"] == "patient.full_name"
    assert "value" not in statuses["name"]
    assert statuses["npi"]["status"] == "unmapped"
    assert statuses["npi"]["canonical"] is None
    assert statuses["skip_me"]["status"] == "other"
    assert "should-not-leak" not in str(body)


def test_mapping_coverage_empty_without_map(tpl_client, admin_api_key, isolated_storage):
    tid = "priv_clinic_no_map_cov"
    TemplateService().add(
        TemplateManifest(id=tid, name="No Map Yet"),
        b"%PDF-1.4\n%%EOF\n",
    )
    r = tpl_client.get(
        f"/api/v1/templates/{tid}/mapping-coverage",
        headers={"X-API-Key": admin_api_key["plain"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_mapping"] is False
    assert body["rows"] == []
    assert body["total_fields"] == 0
    assert body["message"]
