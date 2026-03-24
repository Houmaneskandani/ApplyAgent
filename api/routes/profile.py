from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, BackgroundTasks
from api.auth import get_current_user
from db import get_pool
from matcher import score_jobs
from pydantic import BaseModel
import json
import os

router = APIRouter()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")


def get_supabase():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


class ProfileUpdate(BaseModel):
    name: str | None = None
    preferences: dict = {}


@router.get("/")
async def get_profile(user=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, name, email, resume_url, preferences
            FROM users WHERE id = $1
        """, user["user_id"])
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        result = dict(row)
        prefs = result.get("preferences") or {}
        if isinstance(prefs, str):
            try:
                prefs = json.loads(prefs)
            except Exception:
                prefs = {}
        result["preferences"] = prefs
        return result


@router.put("/")
async def update_profile(data: dict, user=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET name = $1, preferences = $2
            WHERE id = $3
        """, data.get("name"), json.dumps(data.get("preferences", {})), user["user_id"])
        return {"status": "updated"}


@router.post("/resume")
async def upload_resume(
    file: UploadFile = File(...),
    user=Depends(get_current_user)
):
    if not file.filename or not file.filename.lower().endswith((".pdf", ".doc", ".docx")):
        raise HTTPException(status_code=400, detail="Only PDF, DOC, DOCX allowed")

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    content = await file.read()
    supabase = get_supabase()
    file_path = f"{user['user_id']}/resume_{file.filename}"

    supabase.storage.from_("resumes").upload(
        file_path,
        content,
        {"content-type": file.content_type or "application/pdf", "x-upsert": "true"}
    )

    signed = supabase.storage.from_("resumes").create_signed_url(file_path, 60 * 60 * 24 * 365)
    resume_url = signed.get("signedURL") or signed.get("signedUrl") or signed.get("path", "")

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET resume_url = $1 WHERE id = $2",
            resume_url,
            user["user_id"],
        )

    return {"resume_url": resume_url, "filename": file.filename}


@router.post("/rescore")
async def rescore_jobs(background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    """Rescore all jobs based on updated profile preferences."""
    background_tasks.add_task(score_jobs, user["user_id"])
    return {"status": "rescoring started"}


@router.get("/resume/download")
async def get_resume_url(user=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT resume_url FROM users WHERE id = $1", user["user_id"]
        )
        if not row or not row["resume_url"]:
            raise HTTPException(status_code=404, detail="No resume uploaded")
        return {"resume_url": row["resume_url"]}
