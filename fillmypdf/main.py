"""
FillMyPDF Main Application
===========================
Production-ready API with modular OOP architecture
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded

from .api.dependencies.auth import require_admin
from .api.dependencies.rate_limit import limiter
from .api.error_handlers import rate_limit_exceeded_handler, register_exception_handlers
from .api.middleware.request_id import RequestIDMiddleware
from .api.routes import keys, profiles
from .config import settings
from .models import HealthResponse, UsageStats
from .services.account_service import AccountService
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
    from .services.profile_seed import install_demo_profiles
    from .services.trial_seed import install_trial_seed

    try:
        seed_stats = install_trial_seed()
    except Exception as exc:
        print(f"⚠️  Trial seed skipped: {exc}")
        seed_stats = {}
    try:
        profile_seed = install_demo_profiles()
    except Exception as exc:
        print(f"⚠️  Demo profile seed skipped: {exc}")
        profile_seed = {}
    admin_login = AccountService().ensure_admin_user()
    # Fallback only when no operator email is configured and the key store is empty.
    bootstrap_key = None
    if not admin_login:
        bootstrap_key = APIKeyService().bootstrap_admin_key_if_empty()

    # Start background job runner
    if HAS_JOBS:
        get_runner()

    print(f"\n{'='*70}")
    print(f"🚀 {settings.APP_NAME} v{settings.APP_VERSION}")
    print(f"{'='*70}")
    print(f"📁 Storage:        {settings.STORAGE_DIR}")
    if any(seed_stats.get(k, 0) for k in ("templates", "maps")):
        print(
            f"🧪 Trial seed:     "
            f"{seed_stats.get('templates', 0)} PDFs, "
            f"{seed_stats.get('maps', 0)} locked maps"
        )
    if profile_seed.get("created"):
        print(f"👤 Demo profiles:  {profile_seed['created']} shared trial packs")
    print(f"🔐 Encryption:     {'Enabled' if settings.PROFILES_ENCRYPTION_ENABLED else 'Disabled'}")
    print(f"📊 Profile limits: {settings.PROFILE_LIMITS}")
    print(f"📦 Batch:          {'Enabled' if HAS_BATCH else 'Disabled'}")
    print(f"✍️  E-sign overlay: {'Enabled' if HAS_SIGNING else 'Disabled'}")
    print(f"🔑 Auth:           Email/password (cookie) or X-API-Key")
    print(f"⏱️  Rate limits:    {settings.RATE_LIMITS}")
    print(f"🌐 CORS origins:   {settings.CORS_ORIGINS}")
    print(f"📖 API Docs:       http://localhost:{settings.API_PORT}/docs (admin key)")

    if admin_login:
        print(f"\n{'─'*70}")
        print(f"🔐 {admin_login}")
        print(f"   Password is ADMIN_PASSWORD from .env (not printed).")
        print(f"{'─'*70}")
    if bootstrap_key:
        print(f"\n{'─'*70}")
        print(f"🆕 BOOTSTRAP ADMIN KEY (save this — it will NEVER be shown again):")
        print(f"   {bootstrap_key.key}")
        print(f"   ↑ Prefer ADMIN_EMAIL / ADMIN_PASSWORD and /ui/login.html instead.")
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
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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
    # Force the browser to revalidate the UI pages on every load. Static HTML is
    # otherwise heuristically cached (StaticFiles sets no Cache-Control), which
    # left users clicking a stale dashboard after we shipped fixes. "no-cache"
    # still allows a 304 when unchanged, so it's cheap — it just guarantees the
    # latest markup/JS is served after an edit.
    if request.url.path.startswith(("/ui", "/static")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ---------------------------------------------------------------------------
# Admin-only API docs (not shown to clinic logins)
# ---------------------------------------------------------------------------
def _spec_json() -> str:
    return json.dumps(app.openapi()).replace("<", "\\u003c")


def _swagger_html(spec: str) -> str:
    title = f"{settings.APP_NAME} API"
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"/>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({{
      spec: {spec},
      dom_id: "#swagger-ui",
      persistAuthorization: true
    }});
  </script>
</body>
</html>
"""


def _redoc_html(spec: str) -> str:
    title = f"{settings.APP_NAME} API"
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
</head>
<body>
  <div id="redoc-container"></div>
  <script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script>
  <script>
    Redoc.init({spec}, {{}}, document.getElementById("redoc-container"));
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# System routes (no auth required)
# ---------------------------------------------------------------------------
_LANDING_PAGE = Path(__file__).parent / "ui" / "landing.html"


@app.get("/", include_in_schema=False)
async def root():
    """Public marketing landing page. Programmatic service info lives at /status."""
    if _LANDING_PAGE.is_file():
        return FileResponse(_LANDING_PAGE, media_type="text/html")
    return await status()


@app.get("/status", tags=["system"])
async def status():
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


@app.get("/ai-provider", tags=["system"])
async def get_ai_provider_info():
    """
    Returns the active LLM provider configuration (no auth required).
    The UI uses this to pre-select Gemini vs. Local Qwen and show the HIPAA badge.
    """
    from .services.ai_provider import provider_info
    return provider_info()


@app.get("/usage", response_model=UsageStats, tags=["system"])
async def get_usage():
    """Get global API usage statistics (no auth required)"""
    return UsageStats(
        total_requests=usage_stats["total_requests"],
        requests_today=usage_stats["requests_today"],
        profiles_created=usage_stats["profiles_created"],
        last_reset=datetime.fromisoformat(usage_stats["last_reset"]),
    )


@app.get("/openapi.json", include_in_schema=False)
async def openapi_spec(_admin: dict = Depends(require_admin)):
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
async def swagger_docs(_admin: dict = Depends(require_admin)):
    return HTMLResponse(_swagger_html(_spec_json()))


@app.get("/redoc", include_in_schema=False)
async def redoc_docs(_admin: dict = Depends(require_admin)):
    return HTMLResponse(_redoc_html(_spec_json()))


# ---------------------------------------------------------------------------
# Include routers (all require auth via router-level dependency)
# ---------------------------------------------------------------------------
app.include_router(keys.router, prefix="/api/v1")
app.include_router(profiles.router, prefix="/api/v1")
from .api.routes import auth_routes, account_routes
app.include_router(auth_routes.router, prefix="/api/v1")
app.include_router(account_routes.router, prefix="/api/v1")

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

# PDF utilities (merge / split) — always available if pypdf is installed
try:
    from .api.routes import pdf_utils_routes
    app.include_router(pdf_utils_routes.router, prefix="/api/v1")
except ImportError:
    pass

# Billing routes — always included; Stripe features gracefully degrade if not configured
from .api.routes import billing_routes
app.include_router(billing_routes.router, prefix="/api/v1")

# Approval workflow routes
try:
    from .api.routes import approval_routes
    app.include_router(approval_routes.router, prefix="/api/v1")
except ImportError as e:
    print(f"⚠️  Approval routes not available: {e}")

# PA fill routes
try:
    from .api.routes import pa_routes
    app.include_router(pa_routes.router, prefix="/api/v1")
except ImportError as e:
    print(f"⚠️  PA routes not available: {e}")

# Canonical mapping review / approve-lock routes (admin)
try:
    from .api.routes import mapping_review_routes
    app.include_router(mapping_review_routes.router, prefix="/api/v1")
except ImportError as e:
    print(f"⚠️  Mapping review routes not available: {e}")

# ---------------------------------------------------------------------------
# Serve UI static pages at /ui/*
# ---------------------------------------------------------------------------
_UI_DIR = Path(__file__).parent / "ui"
_STATIC_DIR = Path(__file__).parent / "static"

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    path = _UI_DIR / "favicon.ico"
    if path.is_file():
        return FileResponse(path, media_type="image/x-icon")
    return JSONResponse({"detail": "Not found"}, status_code=404)

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
