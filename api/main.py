import asyncio
import os
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# IMPORTANT: validate required env vars BEFORE any route module is imported.
# This guarantees that a misconfigured production deploy crashes immediately
# instead of silently running with insecure defaults.
from config import validate_config, SENTRY_DSN, SENTRY_TRACES_SAMPLE_RATE, APP_ENV
validate_config(strict=True)

# ─── Sentry (optional — set SENTRY_DSN to enable) ──────────────────────
# No-op when DSN is unset, so this is safe for local dev and CI.
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=APP_ENV,
            traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
            integrations=[FastApiIntegration(), AsyncioIntegration()],
            # Enable the new Logs product (Sentry's structured-log surface,
            # ties log lines to the same trace as the exception). Auto-uses
            # the default LoggingIntegration too, so any `logging.getLogger`
            # calls forward; existing `print()` calls still only go to
            # Railway logs (acceptable — refactor incrementally).
            enable_logs=True,
            # Don't include PII in error reports. Sentry's setup wizard
            # suggests `send_default_pii=True`, but we deliberately keep it
            # FALSE — request bodies on /apply contain resume URLs, the
            # /profile PUT contains IMAP creds (pre-encryption), etc.
            send_default_pii=False,
        )
        print(f"  ✓ Sentry enabled (env={APP_ENV}, traces={SENTRY_TRACES_SAMPLE_RATE}, logs=on)")
    except ImportError:
        print("  ⚠ SENTRY_DSN set but sentry-sdk not installed")
    except Exception as e:
        print(f"  ⚠ Sentry init failed: {e}")

from api.auth import router as auth_router, get_current_user  # noqa: E402
from api.routes.jobs import router as jobs_router  # noqa: E402
from api.routes.apply import router as apply_router  # noqa: E402
from api.routes.profile import router as profile_router  # noqa: E402
from api.routes.credits import router as credits_router  # noqa: E402
from api.routes.queue import router as queue_router  # noqa: E402
from api.routes.auto_apply import router as auto_apply_router  # noqa: E402
from fastapi import Depends  # noqa: E402


# Whether the web service should also run the scheduler loop. By default OFF,
# because Railway also runs a dedicated worker service (railway.worker.toml)
# that handles scrape + score. Two services scraping every 6h is a waste of
# Claude tokens, JSearch quota, and DB writes. Set RUN_SCHEDULER_IN_WEB=1
# only if you do NOT deploy the worker.
RUN_SCHEDULER_IN_WEB = os.getenv("RUN_SCHEDULER_IN_WEB", "0") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if RUN_SCHEDULER_IN_WEB:
        from scheduler import scheduler_loop
        task = asyncio.create_task(scheduler_loop())
    yield
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="JobBot API", lifespan=lifespan)

# Wire up auth rate-limiter (slowapi). If the dep isn't installed for any
# reason, the auth router silently runs without limits — but in production
# slowapi is required by requirements.txt.
try:
    from slowapi.errors import RateLimitExceeded
    from slowapi import _rate_limit_exceeded_handler
    from api.auth import limiter as _auth_limiter
    if _auth_limiter is not None:
        app.state.limiter = _auth_limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
except ImportError:
    pass

# Allow localhost dev + any Vercel deployment URL
_extra = os.getenv("ALLOWED_ORIGINS", "")
ORIGINS = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://127.0.0.1:3000",
    "https://apply-agent-frontend.vercel.app",
] + [o.strip() for o in _extra.split(",") if o.strip()]

# SECURITY: explicit method / header allow-lists. The previous "*" wildcard
# combined with allow_credentials=True is over-permissive — it lets any
# origin we allow attempt arbitrary methods (e.g., PATCH for not-yet-existent
# routes) with arbitrary headers (e.g., X-Forwarded-For spoofing). We only
# actually use GET/POST/PUT/DELETE + OPTIONS for preflight, and the only
# headers the frontend sends are Authorization (JWT), Content-Type (JSON or
# multipart), and the standard fetch/XHR set.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Origin",
        "X-Requested-With",
    ],
)

app.include_router(auth_router,        prefix="/auth",         tags=["auth"])
app.include_router(jobs_router,        prefix="/jobs",         tags=["jobs"])
app.include_router(apply_router,       prefix="/apply",        tags=["apply"])
app.include_router(profile_router,     prefix="/profile",      tags=["profile"])
app.include_router(credits_router,     prefix="/credits",      tags=["credits"])
app.include_router(queue_router,       prefix="/queue",        tags=["queue"])
app.include_router(auto_apply_router,  prefix="/auto-apply",   tags=["auto-apply"])


_BOOT_TIME = time.time()


@app.get("/")
async def root():
    return {"status": "JobBot API running"}


@app.get("/sentry-debug")
async def sentry_debug(user=Depends(get_current_user)):
    """
    Verification endpoint for the Sentry integration. Triggers a deliberate
    error so we can confirm events are landing in the Sentry dashboard.

    SECURITY: requires a valid JWT — without auth, anyone on the internet
    could hammer this and burn through the Sentry free-tier event quota
    (or worse, fingerprint our error-reporting setup). Remove this endpoint
    entirely once you've confirmed Sentry is healthy.
    """
    if SENTRY_DSN:
        import sentry_sdk
        # Send a structured log so we can verify the Logs surface too.
        try:
            sentry_sdk.logger.info(
                "sentry-debug hit by user_id=%s", user["user_id"],
            )
        except AttributeError:
            # Older sentry-sdk releases lack the new logger API; ignore.
            pass
    # Trigger an unhandled exception — Sentry will capture this and attach
    # the request transaction.
    division_by_zero = 1 / 0
    return {"ok": True, "trigger": division_by_zero}  # unreachable


@app.get("/health")
async def health():
    """
    Liveness + dependency check. Returns 200 only if the DB ping succeeds.
    Background-task health (scheduler, worker) is NOT in scope here — that
    belongs in a separate /admin/health endpoint protected by the operator.

    Used by Railway healthcheckPath and by uptime monitors. Safe to expose
    publicly because it returns no PII.
    """
    checks: dict[str, dict] = {}
    overall_ok = True

    # Database ping. asyncpg connection pool warm-up time is what we measure.
    try:
        from db import get_pool
        t0 = time.perf_counter()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=3.0)
        checks["database"] = {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    except Exception as e:
        checks["database"] = {"ok": False, "error": type(e).__name__}
        overall_ok = False

    # Required-secret presence (NOT validity — we never call out to verify).
    from config import ANTHROPIC_API_KEY, STRIPE_SECRET_KEY, CAPSOLVER_API_KEY, SECRETS_ENCRYPTION_KEY
    checks["anthropic"]  = {"configured": bool(ANTHROPIC_API_KEY)}
    checks["stripe"]     = {"configured": bool(STRIPE_SECRET_KEY)}
    checks["capsolver"]  = {"configured": bool(CAPSOLVER_API_KEY)}
    checks["secrets_at_rest"] = {"configured": bool(SECRETS_ENCRYPTION_KEY)}

    return {
        "status": "ok" if overall_ok else "degraded",
        "uptime_seconds": round(time.time() - _BOOT_TIME, 1),
        "env": APP_ENV,
        "checks": checks,
    }


@app.get("/stats/public")
async def public_stats():
    """
    Public-safe aggregates used by the marketing page. NEVER returns PII.
    Anyone (including unauthenticated visitors) can call this — the numbers
    here are intentional trust signals.
    """
    try:
        from db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            total_users   = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
            total_jobs    = await conn.fetchval("SELECT COUNT(*) FROM jobs") or 0
            total_applied = await conn.fetchval(
                "SELECT COUNT(*) FROM applications WHERE status = 'applied'"
            ) or 0
            applied_today = await conn.fetchval(
                "SELECT COUNT(*) FROM applications WHERE status = 'applied' "
                "AND applied_at > NOW() - INTERVAL '24 hours'"
            ) or 0
            # Success rate over the last 7 days — bot applies that landed
            # vs. bot applies we tried at all (excludes dry runs and 'new').
            recent_attempts = await conn.fetchval(
                "SELECT COUNT(*) FROM applications "
                "WHERE applied_at > NOW() - INTERVAL '7 days' "
                "AND status IN ('applied', 'failed', 'unknown') "
                "AND dry_run = FALSE"
            ) or 0
            recent_applied = await conn.fetchval(
                "SELECT COUNT(*) FROM applications "
                "WHERE applied_at > NOW() - INTERVAL '7 days' "
                "AND status = 'applied' AND dry_run = FALSE"
            ) or 0
        rate = round(100 * recent_applied / recent_attempts, 1) if recent_attempts else None
        return {
            "users": int(total_users),
            "jobs_indexed": int(total_jobs),
            "applications_submitted": int(total_applied),
            "applications_submitted_today": int(applied_today),
            "success_rate_7d_pct": rate,
        }
    except Exception:
        # Marketing endpoint must NEVER error in a user-facing way.
        return {
            "users": None, "jobs_indexed": None,
            "applications_submitted": None,
            "applications_submitted_today": None,
            "success_rate_7d_pct": None,
        }
