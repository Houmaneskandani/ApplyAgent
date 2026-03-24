from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.auth import router as auth_router
from api.routes.jobs import router as jobs_router
from api.routes.apply import router as apply_router
from api.routes.profile import router as profile_router

app = FastAPI(title="JobBot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # React dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
app.include_router(apply_router, prefix="/apply", tags=["apply"])
app.include_router(profile_router, prefix="/profile", tags=["profile"])


@app.get("/")
async def root():
    return {"status": "JobBot API running"}
