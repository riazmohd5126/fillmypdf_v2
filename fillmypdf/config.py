"""
FillMyPDF Configuration
=======================
Centralized settings using pydantic-settings
"""

from pydantic_settings import BaseSettings
from pathlib import Path
from typing import Dict, List, Optional


class Settings(BaseSettings):
    """Application settings"""
    
    # App Info
    APP_NAME: str = "FillMyPDF"
    APP_VERSION: str = "4.0.0"
    DEBUG: bool = False
    
    # API Settings
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    # CORS: in DEBUG we allow * for local dev convenience.
    # In production, override via .env with an explicit comma-separated list.
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8000"]

    # Storage Paths
    BASE_DIR: Path = Path(__file__).parent
    STORAGE_DIR: Path = BASE_DIR / "storage"
    PROFILES_DIR: Path = STORAGE_DIR / "profiles"
    UPLOAD_DIR: Path = STORAGE_DIR / "temp" / "uploads"
    OUTPUT_DIR: Path = STORAGE_DIR / "temp" / "outputs"
    
    # Profile Settings
    PROFILES_ENCRYPTION_ENABLED: bool = True
    PROFILES_ENCRYPTION_KEY: str = "your-secret-key-change-this-in-production"
    # Per-tier profile limits (-1 = unlimited)
    PROFILE_LIMITS: Dict[str, int] = {
        "free": 1,
        "pro": -1,
        "business": -1,
        "admin": -1,
    }
    # Kept for backward compatibility with older code paths
    PROFILES_FREE_LIMIT: int = 1
    PROFILES_PRO_LIMIT: int = -1
    
    # AI Settings (defaults — Gemini cloud)
    DEFAULT_AI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    DEFAULT_AI_MODEL: str = "gemini-2.5-flash"
    DEFAULT_DPI: int = 200

    # ── Gemini label fallback ("use Gemini where AcroForm can't label") ──────
    # Server-side Gemini key.  When set (here or via the GEMINI_API_KEY env var)
    # AND AI_LABEL_FALLBACK is True, the acroform extract path AUTOMATICALLY
    # sends fields whose printed label geometry couldn't resolve
    # (label_source == "name") to Gemini vision — no per-request ai_labels flag
    # or key needed.  Never fires when AI_LOCAL_ONLY=True (HIPAA guardrail) or
    # for a non-acroform engine.  Leave empty to keep the fallback dormant.
    GEMINI_API_KEY: str = ""
    AI_LABEL_FALLBACK: bool = True

    # Label cache: persist the (expensive) full-Gemini field→label/section/group
    # mapping keyed by the blank form's STRUCTURE (no user data), so a template
    # is labeled by Gemini at most once and every later extract/fill reads it
    # locally — no repeat AI calls, no PHI egress. Delete storage/label_cache/*
    # to force a re-label.
    LABEL_CACHE_ENABLED: bool = True

    # ── LLM Provider toggle ──────────────────────────────────────────────────
    # AI_PROVIDER: "gemini" uses the cloud Gemini endpoint above.
    #              "local"  uses the self-hosted Ollama/vLLM server below.
    #              Per-request ai_provider= form field overrides this for one call.
    AI_PROVIDER: str = "gemini"

    # Local / on-prem server (Ollama default; vLLM uses port 8000)
    # For HIPAA: run Ollama on this machine or point at another host on your LAN.
    LOCAL_AI_BASE_URL: str = "http://localhost:11434/v1"
    # 8GB Mac default — fits comfortably; bump to qwen2.5:7b if you close other apps.
    LOCAL_AI_MODEL: str = "qwen2.5:3b-instruct"
    # Ollama ignores the API key; set any non-empty string.
    LOCAL_AI_API_KEY: str = "ollama"

    # Hard HIPAA guardrail — when True, reject any base_url that resolves to an
    # external host (non-loopback, non-RFC-1918).  Prevents accidental PHI egress.
    AI_LOCAL_ONLY: bool = False

    # Opt-in: include field coordinates (page, x-band, y-band) in the LLM prompt
    # to help disambiguate identically-labeled fields in different form sections.
    # Default OFF so existing behaviour / accuracy is unchanged.  A/B-test on your
    # PA forms and keep only if avg_confidence improves.
    AI_USE_COORDINATES: bool = False

    # ── Field-detection engine ───────────────────────────────────────────────
    # Selects HOW form fields are located + understood for inspection/extraction.
    #   "opencv"    (default) — detect field boxes/checkboxes/underlines from the
    #                rendered page image using OpenCV.  Fully local, no AI, never
    #                calls Gemini.  Boxes are matched back to AcroForm widgets by
    #                overlap so fills keep working; labels reuse the pdfplumber
    #                geometry (or OCR when the page has no text layer).
    #   "vlm_local" — local Qwen2.5-VL reads the page image for label/section/group.
    #                LOCAL ONLY: base_url is hard-pinned to LOCAL_AI_BASE_URL and
    #                asserted private, so it can never egress to Gemini.
    #   "acroform"  — the original pypdf-widget + pdfplumber-geometry pipeline,
    #                kept as a deterministic toggle / regression baseline.
    # Per-request `engine=` on the extract route overrides this for one call.
    # Default is the original AcroForm+geometry pipeline (proven accurate on
    # widget PDFs); "opencv" and "vlm_local" are opt-in toggles.
    FIELD_DETECTION_ENGINE: str = "acroform"

    # DPI used to rasterize pages for the OpenCV / VLM engines.
    CV_DPI: int = 200
    # Run OCR (pytesseract) to recover labels only when a page has no text layer
    # (i.e. scanned image PDFs).  Digital PDFs keep using the free text layer.
    CV_OCR_ENABLED: bool = True
    # Minimum intersection-over-union for binding an OpenCV-detected box to an
    # existing AcroForm widget (so the detected field inherits the real /T name
    # and stays fillable).  Below this, the box keeps a synthesized name.
    CV_MATCH_IOU: float = 0.3
    # Local vision-language model served by Ollama/vLLM for the vlm_local engine.
    # The text LOCAL_AI_MODEL (e.g. qwen2.5:3b) is NOT multimodal — this must be
    # a vision variant.  Pull it first:  ollama pull qwen2.5vl:3b
    # Bump to qwen2.5vl:7b for better accuracy if you have the RAM/VRAM.
    LOCAL_VISION_MODEL: str = "qwen2.5vl:3b"

    # ── Prior-Authorization auto-routing ────────────────────────────────────
    # Templates whose manifest.category is in PA_CATEGORIES are treated as
    # PHI-sensitive.  When PA_FORCE_LOCAL=True the server prefers the local LLM
    # (Ollama/vLLM) for those forms automatically — no per-request flag needed.
    #
    # Behaviour summary:
    #   PA_FORCE_LOCAL=False (default) → behaves exactly like today for all forms.
    #   PA_FORCE_LOCAL=True            → PA templates silently use local Qwen.
    #     Fail-open: if the local server is unreachable the call falls back to
    #     the normal (cloud) resolution so fills keep working.
    #     Fail-closed: set AI_LOCAL_ONLY=True as well to block the cloud fallback.
    #   Per-request ai_provider= always wins over this auto-routing.
    PA_CATEGORIES: list = ["prior_authorization"]
    PA_FORCE_LOCAL: bool = False
    # Seconds to wait when probing the local server for the fail-open check.
    PA_LOCAL_PROBE_TIMEOUT: float = 1.5
    
    # CommonForms Settings (flat PDF -> fillable field detection)
    COMMONFORMS_MODEL: str = "FFDNet-S"
    COMMONFORMS_CONFIDENCE: float = 0.1
    COMMONFORMS_IMAGE_SIZE: int = 1024
    # Use the ONNX/"fast" path (lower memory, CPU-friendly).
    COMMONFORMS_FAST: bool = True

    # Where flat -> fillable conversion runs:
    #   "local" — run commonforms/torch in-process (needs RAM; heavy on 8GB).
    #   "cloud" — offload to a remote converter service (thin clients, no torch).
    # Thin clients (laptop, Chrome extension, other apps) should use "cloud".
    COMMONFORMS_MODE: str = "local"
    # Remote converter endpoint, e.g. "https://convert.example.com/convert".
    CONVERT_SERVICE_URL: str = ""
    # Sent as the X-Convert-Key header to authenticate to the converter.
    CONVERT_SERVICE_KEY: str = ""
    # Seconds to wait for the remote converter before failing over.
    CONVERT_SERVICE_TIMEOUT: float = 120.0

    # Template mapping cache (Layer 3)
    # DEPRECATED: this cache stored FILLED VALUES (PHI) on disk. It is now
    # disabled by default and replaced by FieldMapCache (values-free). Left as
    # a flag only so an operator can consciously re-enable the old behaviour.
    TEMPLATE_CACHE_ENABLED: bool = False
    TEMPLATE_CACHE_TTL_DAYS: int = 0        # 0 = never expire

    # Field-mapping cache (PHI-free): caches only which USER KEY feeds which PDF
    # field (schema), keyed on labels + user keys — never any values. This lets
    # the same form be mapped once and then filled locally with zero PHI ever
    # written to disk or sent to the AI on cache hits.
    FIELD_MAP_CACHE_ENABLED: bool = True

    # ── Canonical fork ("Call 4" at request time) ───────────────────────────
    # When enabled, the autofill pipeline first maps each PDF field to the FIXED
    # canonical schema (pa_canonical.CATALOG) and fills every field it can from
    # canonically-resolved user data — deterministically, with critical-field
    # deferral (a wrong member_id/dob/npi is left blank rather than guessed).
    # Only the fields the canonical fork can't place fall through to the general
    # Call-3 (field→user-key) mapper. Set False to restore pure Call-3 behaviour.
    CANONICAL_FORK_ENABLED: bool = True
    # Cache the field→canonical-path mapping per blank form (PHI-free, keyed on
    # structure + schema version). Built once, reused forever.
    CANONICAL_MAP_CACHE_ENABLED: bool = True
    # Allow the canonical mapper to fall back to the AI (blank-form labels only,
    # PHI-free) for fields resolve_label can't place. One call per form, cached.
    CANONICAL_AI_FALLBACK: bool = True
    # Critical canonical fields (member_id, dob, npi, drug…) are DEFERRED (left
    # blank + reported) unless the mapping confidence meets this bar.
    # Ladder: high=0.9, medium=0.7, low=0.4.
    CANONICAL_CRITICAL_MIN_CONFIDENCE: float = 0.9

    # Async job queue
    JOB_WORKER_THREADS: int = 4       # concurrent batch workers
    JOB_MAX_LISTED: int = 100         # max jobs returned by GET /jobs

    # Outbound webhook HMAC — when set, completion POSTs add X-FillMyPDF-Signature
    # unless the submitter passes an empty webhook_secret and no per-job secret.
    WEBHOOK_SIGNING_SECRET: Optional[str] = None
    # Completion webhook delivery: total HTTP attempts (≥1) with exponential backoff.
    WEBHOOK_MAX_ATTEMPTS: int = 4
    WEBHOOK_RETRY_BASE_DELAY_SEC: float = 1.0

    # Confidence threshold — fields mapped below this score are skipped.
    # 0.0 = write everything the AI returns (old behaviour).
    # 0.5 = skip guesses, keep plausible/certain matches.
    # Override via .env: FILL_CONFIDENCE_THRESHOLD=0.5
    FILL_CONFIDENCE_THRESHOLD: float = 0.0

    # Stripe billing (optional — leave blank to disable checkout/portal)
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""

    # Email / SMTP — for signing notifications (optional)
    # Set SMTP_HOST to enable; leave blank to disable email entirely.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_ADDRESS: str = "noreply@fillmypdf.app"
    SMTP_FROM_NAME: str = "FillMyPDF"
    SMTP_USE_TLS: bool = True
    # Public base URL used to build signing links in emails
    APP_BASE_URL: str = "http://localhost:8000"

    # Rate limits per tier (slowapi syntax: "<count>/<period>")
    # Multiple limits can be combined with semicolons.
    RATE_LIMITS: Dict[str, str] = {
        "free":     "60/minute;10000/day",
        "pro":      "600/minute;100000/day",
        "business": "6000/minute;1000000/day",
        "admin":    "100000/minute",
    }
    # Auth bypass paths (no API key required). Used by main.py.
    AUTH_BYPASS_PATHS: List[str] = [
        "/", "/health", "/usage",
        "/docs", "/redoc", "/openapi.json",
    ]
    
    class Config:
        env_file = ".env"
        case_sensitive = True
    
    _DEFAULT_KEY = "your-secret-key-change-this-in-production"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Create directories on init
        self.STORAGE_DIR.mkdir(exist_ok=True, parents=True)
        self.PROFILES_DIR.mkdir(exist_ok=True, parents=True)
        self.UPLOAD_DIR.mkdir(exist_ok=True, parents=True)
        self.OUTPUT_DIR.mkdir(exist_ok=True, parents=True)
        # Warn if running with the default insecure encryption key
        if self.PROFILES_ENCRYPTION_ENABLED and self.PROFILES_ENCRYPTION_KEY == self._DEFAULT_KEY:
            import warnings
            warnings.warn(
                "PROFILES_ENCRYPTION_KEY is set to the default insecure value. "
                "Set a strong random key in your .env file before storing real data.",
                UserWarning,
                stacklevel=2,
            )


# Global settings instance
settings = Settings()
