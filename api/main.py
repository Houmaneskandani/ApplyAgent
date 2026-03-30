import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.auth import router as auth_router
from api.routes.jobs import router as jobs_router
from api.routes.apply import router as apply_router
from api.routes.profile import router as profile_router
from api.routes.credits import router as credits_router
from api.routes.queue import router as queue_router
from api.routes.auto_apply import router as auto_apply_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    from scheduler import scheduler_loop
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="JobBot API", lifespan=lifespan)

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
