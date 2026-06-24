"""
Unit tests for fillmypdf.services.ai_provider
==============================================
Covers:
  - resolve_ai_config: gemini default, local mode (server-level and per-request)
  - assert_egress_allowed: allows loopback/private, rejects external in local-only mode
  - _build_labeled_fields: default (no coords) and coordinate-enhanced mode
"""

from __future__ import annotations

import pytest

from fillmypdf.services.ai_provider import assert_egress_allowed, prepare_ai_config, resolve_ai_config
from fillmypdf.services.vision_service import _build_labeled_fields


# ---------------------------------------------------------------------------
# Fixtures — reset settings to known state before each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    """Ensure tests start from a clean settings baseline."""
    from fillmypdf import config as cfg
    monkeypatch.setattr(cfg.settings, "AI_PROVIDER", "gemini")
    monkeypatch.setattr(cfg.settings, "AI_LOCAL_ONLY", False)
    monkeypatch.setattr(cfg.settings, "LOCAL_AI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(cfg.settings, "LOCAL_AI_MODEL", "qwen2.5:3b-instruct")
    monkeypatch.setattr(cfg.settings, "LOCAL_AI_API_KEY", "ollama")
    monkeypatch.setattr(cfg.settings, "DEFAULT_AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    monkeypatch.setattr(cfg.settings, "DEFAULT_AI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(cfg.settings, "AI_USE_COORDINATES", False)


# ---------------------------------------------------------------------------
# resolve_ai_config
# ---------------------------------------------------------------------------

class TestResolveAiConfig:
    def test_gemini_default_uses_request_key(self):
        key, url, model = resolve_ai_config(request_api_key="my-gemini-key")
        assert key == "my-gemini-key"
        assert "gemini" in url or "google" in url
        assert "gemini" in model

    def test_gemini_default_falls_back_to_settings(self):
        key, url, model = resolve_ai_config()
        assert url.startswith("https://")
        assert "gemini" in model

    def test_local_mode_via_server_setting(self, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_PROVIDER", "local")
        key, url, model = resolve_ai_config(request_api_key="some-cloud-key")
        # Cloud key must be ignored; local settings must be used
        assert url == "http://localhost:11434/v1"
        assert "qwen" in model.lower()
        assert key == "ollama"

    def test_local_mode_via_per_request_hint(self):
        key, url, model = resolve_ai_config(
            request_api_key="some-cloud-key",
            provider_hint="local",
        )
        assert url == "http://localhost:11434/v1"
        assert "qwen" in model.lower()

    def test_gemini_hint_overrides_local_server_setting(self, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_PROVIDER", "local")
        key, url, model = resolve_ai_config(
            request_api_key="my-gemini-key",
            provider_hint="gemini",
        )
        assert key == "my-gemini-key"
        assert "google" in url or "gemini" in url or "googleapis" in url

    def test_custom_base_url_and_model_respected_in_cloud_mode(self):
        key, url, model = resolve_ai_config(
            request_api_key="k",
            request_base_url="https://my-custom-llm.example.com/v1",
            request_model="my-fine-tuned-model",
        )
        assert url == "https://my-custom-llm.example.com/v1"
        assert model == "my-fine-tuned-model"

    def test_local_mode_ignores_custom_base_url(self):
        """In local mode the request base_url must be ignored."""
        key, url, model = resolve_ai_config(
            request_base_url="https://attacker.example.com/v1",
            provider_hint="local",
        )
        assert "localhost" in url or "11434" in url


class TestPrepareAiConfig:
    def test_cloud_mode_requires_key(self):
        with pytest.raises(ValueError, match="ai_api_key is required"):
            prepare_ai_config()

    def test_cloud_mode_allows_missing_key_when_not_required(self):
        key, url, model = prepare_ai_config(require_cloud_key=False)
        assert key == ""
        assert url.startswith("https://")

    def test_local_mode_never_requires_key(self):
        key, url, model = prepare_ai_config(provider_hint="local")
        assert "qwen" in model.lower() or "localhost" in url


# ---------------------------------------------------------------------------
# assert_egress_allowed
# ---------------------------------------------------------------------------

class TestAssertEgressAllowed:
    def test_always_passes_when_local_only_false(self):
        # No exception even for external URLs
        assert_egress_allowed("https://evil.example.com/v1")

    def test_loopback_allowed_in_local_only_mode(self, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_LOCAL_ONLY", True)
        assert_egress_allowed("http://localhost:11434/v1")
        assert_egress_allowed("http://127.0.0.1:8000/v1")

    def test_rfc1918_allowed_in_local_only_mode(self, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_LOCAL_ONLY", True)
        assert_egress_allowed("http://192.168.1.42:11434/v1")
        assert_egress_allowed("http://10.0.0.5:8000/v1")
        assert_egress_allowed("http://172.16.0.1:8080/v1")

    def test_external_host_blocked_in_local_only_mode(self, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_LOCAL_ONLY", True)
        with pytest.raises(ValueError, match="AI_LOCAL_ONLY"):
            assert_egress_allowed("https://generativelanguage.googleapis.com/v1beta/openai/")

    def test_external_ip_blocked_in_local_only_mode(self, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_LOCAL_ONLY", True)
        with pytest.raises(ValueError):
            assert_egress_allowed("http://8.8.8.8:11434/v1")

    def test_gemini_url_blocked_in_local_only_mode(self, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_LOCAL_ONLY", True)
        with pytest.raises(ValueError):
            assert_egress_allowed("https://api.openai.com/v1")

    def test_dot_local_hostname_allowed_in_local_only_mode(self, monkeypatch):
        """A .local hostname (e.g. Ollama on a LAN box) should be allowed."""
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_LOCAL_ONLY", True)
        assert_egress_allowed("http://my-gpu-box.local:11434/v1")
        assert_egress_allowed("http://gpu-server.internal:8000/v1")

    def test_arbitrary_public_hostname_blocked_in_local_only_mode(self, monkeypatch):
        """Any unrecognised hostname that isn't .local/.internal is blocked."""
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_LOCAL_ONLY", True)
        with pytest.raises(ValueError):
            assert_egress_allowed("http://some-cloud-provider.example.com/v1")


# ---------------------------------------------------------------------------
# _build_labeled_fields — coordinate enhancement
# ---------------------------------------------------------------------------

SAMPLE_FIELDS = [
    {"name": "PatientName", "type": "/Tx", "page": 0, "x0": 100, "x1": 300, "x": 200, "y": 50},
    {"name": "PatientPhone", "type": "/Tx", "page": 0, "x0": 100, "x1": 300, "x": 200, "y": 200},
    {"name": "PhysicianPhone", "type": "/Tx", "page": 0, "x0": 400, "x1": 600, "x": 500, "y": 200},
    {"name": "Consent", "type": "/Btn", "page": 0, "x0": 50, "x1": 70, "x": 60, "y": 300},
]
SAMPLE_LABELS = {
    "PatientName": "Patient Name",
    "PatientPhone": "Phone",
    "PhysicianPhone": "Phone",
    "Consent": "I agree",
}


class TestBuildLabeledFields:
    def test_default_no_coordinates(self):
        rows = _build_labeled_fields(SAMPLE_FIELDS, SAMPLE_LABELS)
        assert len(rows) == 4
        assert rows[0] == {"field_name": "PatientName", "type": "textbox", "label": "Patient Name"}
        # No page or position keys
        assert "page" not in rows[0]
        assert "position" not in rows[0]

    def test_checkbox_type_detected(self):
        rows = _build_labeled_fields(SAMPLE_FIELDS, SAMPLE_LABELS)
        consent = next(r for r in rows if r["field_name"] == "Consent")
        assert consent["type"] == "checkbox"

    def test_with_coordinates_enabled(self, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_USE_COORDINATES", True)
        rows = _build_labeled_fields(SAMPLE_FIELDS, SAMPLE_LABELS)
        assert len(rows) == 4
        for row in rows:
            assert "page" in row
            assert "position" in row
            assert "x:" in row["position"]
            assert "y:" in row["position"]

    def test_two_phone_fields_get_different_x_positions(self, monkeypatch):
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "AI_USE_COORDINATES", True)
        rows = _build_labeled_fields(SAMPLE_FIELDS, SAMPLE_LABELS)
        patient_phone = next(r for r in rows if r["field_name"] == "PatientPhone")
        physician_phone = next(r for r in rows if r["field_name"] == "PhysicianPhone")
        # PatientPhone is at x=100 (low), PhysicianPhone at x=400 (high)
        assert patient_phone["position"] != physician_phone["position"]

    def test_missing_label_falls_back_to_field_name(self):
        rows = _build_labeled_fields(SAMPLE_FIELDS, {})
        assert rows[0]["label"] == "PatientName"

    def test_coordinate_mode_off_unchanged_output(self):
        rows_off = _build_labeled_fields(SAMPLE_FIELDS, SAMPLE_LABELS)
        assert all("position" not in r for r in rows_off)


# ---------------------------------------------------------------------------
# PA auto-routing: resolve_provider_for_category
# ---------------------------------------------------------------------------

from fillmypdf.services.ai_provider import resolve_provider_for_category


class TestResolveProviderForCategory:
    """
    Tests for the PA auto-routing helper.

    The helper decides which provider hint to use based on the template's
    category and whether PA_FORCE_LOCAL is enabled.
    """

    def _patch_pa(self, monkeypatch, *, force_local: bool, reachable: bool):
        """Helper: configure PA settings and stub the local-server probe."""
        from fillmypdf import config as cfg
        import fillmypdf.services.ai_provider as module

        monkeypatch.setattr(cfg.settings, "PA_FORCE_LOCAL", force_local)
        monkeypatch.setattr(cfg.settings, "PA_CATEGORIES", ["prior_authorization"])
        monkeypatch.setattr(cfg.settings, "LOCAL_AI_BASE_URL", "http://localhost:11434/v1")
        monkeypatch.setattr(cfg.settings, "PA_LOCAL_PROBE_TIMEOUT", 0.1)
        # Stub the network probe so tests are fast and hermetic
        monkeypatch.setattr(module, "_local_server_reachable", lambda *a, **kw: reachable)

    # ── Rule 1: explicit hint always wins ─────────────────────────────────

    def test_explicit_hint_wins_over_pa_routing(self, monkeypatch):
        """If caller passes ai_provider=gemini on a PA form, respect it."""
        self._patch_pa(monkeypatch, force_local=True, reachable=True)
        result = resolve_provider_for_category("prior_authorization", "gemini")
        assert result == "gemini"

    def test_explicit_local_hint_passes_through(self, monkeypatch):
        self._patch_pa(monkeypatch, force_local=False, reachable=False)
        result = resolve_provider_for_category("prior_authorization", "local")
        assert result == "local"

    # ── Rule 2: PA auto-routing ───────────────────────────────────────────

    def test_pa_category_local_reachable_returns_local(self, monkeypatch):
        """PA form + PA_FORCE_LOCAL=True + server reachable → force local."""
        self._patch_pa(monkeypatch, force_local=True, reachable=True)
        result = resolve_provider_for_category("prior_authorization", None)
        assert result == "local"

    def test_pa_category_local_unreachable_fails_open(self, monkeypatch):
        """PA form + PA_FORCE_LOCAL=True + server down → fail-open (None)."""
        self._patch_pa(monkeypatch, force_local=True, reachable=False)
        result = resolve_provider_for_category("prior_authorization", None)
        assert result is None  # None means normal (cloud) resolution

    def test_pa_force_local_off_no_routing(self, monkeypatch):
        """PA_FORCE_LOCAL=False → no auto-routing even for PA category."""
        self._patch_pa(monkeypatch, force_local=False, reachable=True)
        result = resolve_provider_for_category("prior_authorization", None)
        assert result is None  # unchanged from input

    # ── Rule 3: non-PA category unchanged ────────────────────────────────

    def test_generic_category_not_routed(self, monkeypatch):
        """Non-PA template (e.g. 'general') is never auto-routed."""
        self._patch_pa(monkeypatch, force_local=True, reachable=True)
        result = resolve_provider_for_category("general", None)
        assert result is None

    def test_commercial_insurance_not_routed(self, monkeypatch):
        self._patch_pa(monkeypatch, force_local=True, reachable=True)
        result = resolve_provider_for_category("commercial_insurance", None)
        assert result is None

    def test_none_category_not_routed(self, monkeypatch):
        self._patch_pa(monkeypatch, force_local=True, reachable=True)
        result = resolve_provider_for_category(None, None)
        assert result is None

    # ── prepare_ai_config end-to-end ──────────────────────────────────────

    def test_prepare_ai_config_pa_returns_local_triple(self, monkeypatch):
        """
        End-to-end: prepare_ai_config with a PA category + PA_FORCE_LOCAL=True
        returns the local (key, base_url, model) triple.
        """
        self._patch_pa(monkeypatch, force_local=True, reachable=True)
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "LOCAL_AI_API_KEY", "ollama")
        monkeypatch.setattr(cfg.settings, "LOCAL_AI_MODEL", "qwen2.5:3b-instruct")

        key, url, model = prepare_ai_config(
            request_api_key=None,
            provider_hint=None,
            category="prior_authorization",
            require_cloud_key=False,
        )
        assert key == "ollama"
        assert "localhost" in url
        assert model == "qwen2.5:3b-instruct"

    def test_prepare_ai_config_generic_unchanged(self, monkeypatch):
        """Generic category → no change even when PA_FORCE_LOCAL is on."""
        self._patch_pa(monkeypatch, force_local=True, reachable=True)
        from fillmypdf import config as cfg
        monkeypatch.setattr(cfg.settings, "DEFAULT_AI_MODEL", "gemini-2.5-flash")
        monkeypatch.setattr(
            cfg.settings,
            "DEFAULT_AI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
        )

        key, url, model = prepare_ai_config(
            request_api_key="my-gemini-key",
            provider_hint=None,
            category="general",
            require_cloud_key=False,
        )
        assert "generativelanguage" in url
        assert model == "gemini-2.5-flash"
