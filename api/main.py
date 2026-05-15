import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# IMPORTANT: validate required env vars BEFORE any route module is imported.
# This guarantees that a misconfigured production deploy crashes immediately
# instead of silently running with insecure defaults.
from config import validate_config
validate_config(strict=True)

from api.auth import router as auth_router  # noqa: E402  (imports must follow validation)
from api.routes.jobs import router as jobs_router  # noqa: E402
from api.routes.apply import router as apply_router  # noqa: E402
from api.routes.profile import router as profile_router  # noqa: E402
from api.routes.credits import router as credits_router  # noqa: E402
from api.routes.queue import router as queue_router  # noqa: E402
from api.routes.auto_apply import router as auto_apply_router  # noqa: E402


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router,        prefix="/auth",         tags=["auth"])
app.include_router(jobs_router,        prefix="/jobs",         tags=["jobs"])
app.include_router(apply_router,       prefix="/apply",        tags=["apply"])
app.include_router(profile_router,     prefix="/profile",      tags=["profile"])
app.include_router(credits_router,     prefix="/credits",      tags=["credits"])
app.include_router(queue_router,       prefix="/queue",        tags=["queue"])
app.include_router(auto_apply_router,  prefix="/auto-apply",   tags=["auto-apply"])


@app.get("/")
async def root():
    return {"status": "JobBot API running"}
