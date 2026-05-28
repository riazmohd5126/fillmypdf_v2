"""
FillMyPDF Main Application
===========================
Production-ready API with modular OOP architecture
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded

from .api.dependencies.rate_limit import limiter
from .api.error_handlers import rate_limit_exceeded_handler, register_exception_handlers
from .api.middleware.request_id import RequestIDMiddleware
from .api.routes import keys, profiles
from .config import settings
from .models import HealthResponse, UsageStats
from .services.api_key_service import APIKeyService

# Import batch routes
try:
    from .api.routes import batch_routes
    HAS_BATCH = True
except ImportError as e:
    HAS_BATCH = False
    print(f"⚠️  Batch routes not available: {e}")

# Import template library routes
try:
    from .api.routes import templates as template_routes
    HAS_TEMPLATES = True
except ImportError as e:
    HAS_TEMPLATES = False
    print(f"⚠️  Template routes not available: {e}")

# Import job queue routes
try:
    from .api.routes import jobs as job_routes
    from .services.job_runner import get_runner, shutdown_runner
    HAS_JOBS = True
except ImportError as e:
    HAS_JOBS = False
    print(f"⚠️  Job routes not available: {e}")

# Extract (AcroForm → JSON / CSV)
try:
    from .api.routes import extract_routes
    HAS_EXTRACT = True
except ImportError as e:
    HAS_EXTRACT = False
    print(f"⚠️  Extract routes not available: {e}")

# Visual e-sign — resolved at startup inside lifespan
HAS_SIGNING = False

# ---------------------------------------------------------------------------
# Persistent usage stats (survives restarts via JSON file)
# ---------------------------------------------------------------------------
_STATS_FILE = settings.STORAGE_DIR / "usage_stats.json"


def _load_stats() -> dict:
    if _STATS_FILE.exists():
        try:
            return json.loads(_STATS_FILE.read_text())
        except Exception:
            pass
    return {
        "total_requests": 0,
        "requests_today": 0,
        "profiles_created": 0,
        "batches_processed": 0,
        "last_reset": datetime.now().isoformat(),
    }


def _save_stats(stats: dict) -> None:
    try:
        _STATS_FILE.write_text(json.dumps(stats, default=str))
    except Exception as e:
        print(f"Could not persist usage stats: {e}")


usage_stats = _load_stats()


def track_usage() -> None:
    global usage_stats
    now = datetime.now()
    last = datetime.fromisoformat(usage_stats["last_reset"])
    if now.date() > last.date():
        usage_stats["requests_today"] = 0
        usage_stats["last_reset"] = now.isoformat()
    usage_stats["total_requests"] += 1
    usage_stats["requests_today"] += 1
    _save_stats(usage_stats)


def increment_profiles_created() -> None:
    usage_stats["profiles_created"] = usage_stats.get("profiles_created", 0) + 1
    _save_stats(usage_stats)


def increment_batches_processed() -> None:
    usage_stats["batches_processed"] = usage_stats.get("batches_processed", 0) + 1
    _save_stats(usage_stats)


# Make increment helpers available to route modules
profiles.increment_profiles_created = increment_profiles_created


# ---------------------------------------------------------------------------
# Lifespan (modern replacement for deprecated @app.on_event)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bootstrap: if no API keys exist, create an admin key and print it
    bootstrap_key = APIKeyService().bootstrap_admin_key_if_empty()

    # Start background job runner
    if HAS_JOBS:
        get_runner()

    print(f"\n{'='*70}")
    print(f"🚀 {settings.APP_NAME} v{settings.APP_VERSION}")
    print(f"{'='*70}")
    print(f"📁 Storage:        {settings.STORAGE_DIR}")
    print(f"🔐 Encryption:     {'Enabled' if settings.PROFILES_ENCRYPTION_ENABLED else 'Disabled'}")
    print(f"📊 Profile limits: {settings.PROFILE_LIMITS}")
    print(f"📦 Batch:          {'Enabled' if HAS_BATCH else 'Disabled'}")
    print(f"✍️  E-sign overlay: {'Enabled' if HAS_SIGNING else 'Disabled'}")
    print(f"🔑 Auth:           Enabled (X-API-Key required)")
    print(f"⏱️  Rate limits:    {settings.RATE_LIMITS}")
    print(f"🌐 CORS origins:   {settings.CORS_ORIGINS}")
    print(f"📖 API Docs:       http://localhost:{settings.API_PORT}/docs")

    if bootstrap_key:
        print(f"\n{'─'*70}")
        print(f"🆕 BOOTSTRAP ADMIN KEY (save this — it will NEVER be shown again):")
        print(f"   {bootstrap_key.key}")
        print(f"   ↑ Use this key in the 'X-API-Key' header to call any endpoint.")
        print(f"{'─'*70}")
    print(f"{'='*70}\n")

    yield

    # Shutdown
    if HAS_JOBS:
        shutdown_runner()
    _save_stats(usage_stats)
    print(f"\n👋 {settings.APP_NAME} shutting down...")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
_OPENAPI_TAGS = [
    {
        "name": "profiles",
        "description": "Encrypted profiles and merge-ready field bags for batches.",
    },
    {
        "name": "batch",
        "description": "Synchronous fills: JSON, CSV, Excel — returns when processing finishes.",
    },
    {
        "name": "templates",
        "description": "Stored library PDFs and single-/multi-record fills keyed by ``template_id``.",
    },
    {
        "name": "jobs",
        "description": (
            "Async queue: submitted work returns ``202``; poll ``GET /jobs/{job_id}`` or use webhooks "
            "(HMAC optional, retries + manual replay)."
        ),
    },
    {"name": "extract", "description": "AcroForm field dumps to JSON or CSV."},
    {
        "name": "signing",
        "description": "Visual signature overlay (PNG stamp — not certificate-based PAdES).",
    },
    {
        "name": "api-keys",
        "description": "Administrative API-key lifecycle for ``X-API-Key`` auth.",
    },
    {"name": "system", "description": "Health and usage probes (typically unauthenticated where noted)."},
]

app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "AI-assisted PDF filling: synchronous batch/template routes plus optional **async jobs** "
        "(``POST /api/v1/jobs/...``) with progress polling and completion webhooks."
    ),
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=_OPENAPI_TAGS,
    lifespan=lifespan,
)

# Rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Structured errors + request correlation IDs
register_exception_handlers(app)

# CORS (inner middleware — executes after correlation ID)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Outermost middleware: always populates request.state.request_id
app.add_middleware(RequestIDMiddleware)


# ---------------------------------------------------------------------------
# Middleware: count every non-docs request
# ---------------------------------------------------------------------------
@app.middleware("http")
async def count_requests(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith(("/docs", "/openapi", "/redoc")):
        track_usage()
    return response


# ---------------------------------------------------------------------------
# System routes (no auth required)
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "dashboard": "/dashboard",
        "features": {
            "profiles": True,
            "batch_processing": HAS_BATCH,
            "form_field_inspection": HAS_BATCH,
            "template_library": HAS_TEMPLATES,
            "async_jobs": HAS_JOBS,
            "webhooks": HAS_JOBS,
            "webhooks_manual_redelivery": HAS_JOBS,
            "webhooks_hmac": HAS_JOBS,
            "async_extract_jobs": HAS_JOBS,
            "jobs_list_filters": HAS_JOBS,
            "smart_extraction": HAS_EXTRACT,
            "esign_visual": HAS_SIGNING,
            "dashboard_ui": True,
            "authentication": True,
            "openapi_tagged_sections": True,
            "openapi_schema_examples": True,
            "rate_limiting": True,
        },
    }


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health_check():
    """Health check endpoint (no auth required)"""
    return HealthResponse(status="healthy", version=settings.APP_VERSION)


@app.get("/usage", response_model=UsageStats, tags=["system"])
async def get_usage():
    """Get global API usage statistics (no auth required)"""
    return UsageStats(
        total_requests=usage_stats["total_requests"],
        requests_today=usage_stats["requests_today"],
        profiles_created=usage_stats["profiles_created"],
        last_reset=datetime.fromisoformat(usage_stats["last_reset"]),
    )


# ---------------------------------------------------------------------------
# Include routers (all require auth via router-level dependency)
# ---------------------------------------------------------------------------
app.include_router(keys.router, prefix="/api/v1")
app.include_router(profiles.router, prefix="/api/v1")

if HAS_BATCH:
    app.include_router(batch_routes.router, prefix="/api/v1")
    try:
        from .api.routes import signing_routes
        from .api.routes import signing_session_routes

        app.include_router(signing_routes.router, prefix="/api/v1")
        app.include_router(signing_session_routes.router, prefix="/api/v1")
        HAS_SIGNING = True
    except ImportError as e:
        print(f"⚠️  Signing routes not available: {e}")

if HAS_TEMPLATES:
    app.include_router(template_routes.router, prefix="/api/v1")

if HAS_JOBS:
    app.include_router(job_routes.router, prefix="/api/v1")

if HAS_EXTRACT:
    app.include_router(extract_routes.router, prefix="/api/v1")

# ---------------------------------------------------------------------------
# Serve UI static pages at /ui/*
# ---------------------------------------------------------------------------
_UI_DIR = Path(__file__).parent / "ui"
_STATIC_DIR = Path(__file__).parent / "static"
if _UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "fillmypdf.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.DEBUG,
    )
