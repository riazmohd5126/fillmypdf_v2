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
    
    # AI Settings (defaults)
    DEFAULT_AI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    DEFAULT_AI_MODEL: str = "gemini-2.5-flash"
    DEFAULT_DPI: int = 200
    
    # CommonForms Settings
    COMMONFORMS_MODEL: str = "FFDNet-S"
    COMMONFORMS_CONFIDENCE: float = 0.1
    COMMONFORMS_IMAGE_SIZE: int = 1024

    # Template mapping cache (Layer 3)
    TEMPLATE_CACHE_ENABLED: bool = True
    TEMPLATE_CACHE_TTL_DAYS: int = 0        # 0 = never expire

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
