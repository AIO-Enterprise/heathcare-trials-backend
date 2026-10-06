"""
Main Application Entry Point
Registers all route modules and initializes the database.
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes.account import auth, company, users
from app.api.routes.assets import brand_kit, documents, onboarding
from app.api.routes.campaigns.advertisements import routers as _ad_routers
from app.api.routes.engagement import bookings, chat, sms, survey_responses
from app.api.routes.engagement.hybrid_voice_session import router as hybrid_voice_router
from app.api.routes.insights import analytics
from app.api.routes.integrations.platform_connections import router as connections_router
from app.api.routes.publishing.ads_google import router as google_router
from app.api.routes.publishing.ads_google_manage import router as google_manage_router
from app.core.config import settings
from app.core.exceptions import AppError
from app.core.ratelimit import rate_limit_middleware
from app.db.database import init_db
from app.services.ads.scheduling.google_scheduler import run_google_pause_scheduler
from app.services.ads.scheduling.meta_scheduler import run_pause_scheduler
from app.services.messaging.workers.sms_worker import (
    resume_running_campaigns as resume_sms_campaigns,
)
from app.services.messaging.workers.sms_worker import (
    run_reminder_sweeper,
)
from app.services.voice.campaign_worker import resume_running_campaigns, run_stale_sweeper

# ─── Logging ──────────────────────────────────────────────────────────────────
# App logs stay at INFO in all environments — publish checkpoints, scheduler
# actions and other audit-relevant lines must survive in prod. Only per-request
# access noise (uvicorn.access) is silenced outside DEBUG.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)
if not settings.DEBUG:
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _validate_secret_key() -> None:
    """Refuse to boot production with a forgeable JWT secret."""
    insecure = ("CHANGE-ME", "secret", "dev-insecure")
    if not settings.DEBUG and (
        not settings.SECRET_KEY or len(settings.SECRET_KEY) < 32 or any(s in settings.SECRET_KEY for s in insecure)
    ):
        raise RuntimeError(
            "SECRET_KEY is missing, too short (<32 chars), or a known default. "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )


def _supervised(factory):
    """
    Wrap a long-lived worker coroutine factory so an escaped exception is
    logged loudly instead of killing the loop silently until restart.
    """

    async def _runner():
        while True:
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker %s crashed — restarting in 5s", factory.__name__)
                await asyncio.sleep(5)

    return _runner


async def _security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # HSTS only makes sense when served over HTTPS — skip if no HTTPS configured.
    if not settings.DEBUG and os.getenv("ENABLE_HSTS", "false").lower() == "true":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Hosted landing pages, the website preview, and document file endpoints
    # may be framed (PDF/text preview in an <iframe>) — skip X-Frame-Options.
    _path = request.url.path
    _frameable = (
        _path.endswith("/website") or _path.endswith("/file")  # /{ad_id}/documents/{doc_id}/file and company doc files
    )
    if not _frameable:
        response.headers["X-Frame-Options"] = "DENY"
    # Hosted landing pages are regenerated / patched in place, so a stale browser
    # cache means a re-published page silently won't load (e.g. an old page posting
    # the wrong field to /voice-call/request). Force revalidation for them.
    if "/static/pages/" in _path:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — validate config, create DB tables, ensure storage directories exist
    _validate_secret_key()
    await init_db()
    os.makedirs(os.path.join(settings.STATIC_DIR, "logos"), exist_ok=True)
    os.makedirs(os.path.join(settings.STATIC_DIR, "pages"), exist_ok=True)
    os.makedirs(os.path.join(settings.UPLOAD_DIR, "logos"), exist_ok=True)
    os.makedirs(os.path.join(settings.UPLOAD_DIR, "docs"), exist_ok=True)
    os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
    logger.info("BACKEND_PUBLIC_URL = %r", settings.BACKEND_PUBLIC_URL)
    logger.info("META_OAUTH_REDIRECT_URI = %r", settings.META_OAUTH_REDIRECT_URI)

    from app.services.ads.meta_stale_sweeper import run_stale_generating_sweeper

    scheduler_task = asyncio.create_task(_supervised(run_pause_scheduler)())
    google_scheduler_task = asyncio.create_task(_supervised(run_google_pause_scheduler)())
    sweeper_task = asyncio.create_task(_supervised(run_stale_sweeper)())
    sms_reminder_task = asyncio.create_task(_supervised(run_reminder_sweeper)())
    generating_sweeper_task = asyncio.create_task(_supervised(run_stale_generating_sweeper)())
    await resume_running_campaigns()
    await resume_sms_campaigns()
    yield
    # Shutdown — cancel background tasks gracefully
    for t in (scheduler_task, google_scheduler_task, sweeper_task, sms_reminder_task, generating_sweeper_task):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Marketing Analytics Platform",
    description="AI-powered marketing automation with human-in-the-loop governance",
    version="1.0.0",
    lifespan=lifespan,
    # Hide API docs in production
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)


async def _request_id(request: Request, call_next):
    """Propagate or mint an X-Request-ID for cross-log tracing."""
    request.state.request_id = request.headers.get("x-request-id", "")[:128] or uuid.uuid4().hex
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


# Execution order: middlewares run in reverse registration order, so
# _request_id (registered last) is outermost and runs first — every response,
# including 429s from the inner rate limiter, carries the request ID.
# _security_headers sits in the middle so it also covers rate-limited replies.
app.middleware("http")(rate_limit_middleware)
app.middleware("http")(_security_headers)
app.middleware("http")(_request_id)


# ─── Typed application errors → consistent JSON responses ────────────────────
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "error": exc.code},
    )


# Unhandled exceptions — never leak stack traces to clients.
@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# CORS — open to all origins.
# Real security is enforced by JWT on every request; CORS is browser-level only.
# allow_credentials must be False when allow_origins=["*"] (Starlette constraint).
# The frontend uses Authorization: Bearer <token>, not cookies, so credentials
# mode is never 'include' — False is correct here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Static file serving — logos and brand assets under ./static/
os.makedirs(settings.STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")

# Upload file serving — documents and logos under ./uploads/
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")

# Output file serving — generated creatives under ./outputs/
os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=settings.OUTPUT_DIR), name="outputs")

# ─── Register Route Modules ──────────────────────────────────────────────────

app.include_router(auth.router, prefix="/api")
app.include_router(onboarding.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
for _r in _ad_routers:
    app.include_router(_r, prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(brand_kit.router, prefix="/api")
app.include_router(company.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(bookings.router, prefix="/api")
app.include_router(sms.router, prefix="/api")
app.include_router(survey_responses.router, prefix="/api")
app.include_router(connections_router, prefix="/api")
app.include_router(google_router, prefix="/api")
app.include_router(google_manage_router, prefix="/api")
app.include_router(hybrid_voice_router, prefix="/api", tags=["Hybrid Voice"])


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}
