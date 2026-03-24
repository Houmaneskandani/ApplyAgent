from fastapi import APIRouter, Depends
from api.auth import get_current_user
from db import get_pool

router = APIRouter()


def detect_experience_level(title: str, description: str = "") -> str:
    text = f"{title} {description}".lower()
    if any(w in text for w in ["staff", "principal", "distinguished", "fellow"]):
        return "Staff / Principal"
    if any(w in text for w in ["senior", "sr.", "sr ", "lead", "manager"]):
        return "Senior"
    if any(w in text for w in ["mid", "mid-level", "intermediate", "ii", "iii"]):
        return "Mid Level"
    if any(w in text for w in ["junior", "jr.", "jr ", "associate", "entry", "new grad", "graduate", "intern"]):
        return "Junior"
    return "Mid Level"  # default


def time_ago(dt) -> str:
    if not dt:
        return "Recently"
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if hasattr(dt, "tzinfo") and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    days = diff.days
    hours = diff.seconds // 3600
    if days == 0:
        if hours == 0:
            return "Just now"
        return f"{hours}h ago"
    if days == 1:
        return "1d ago"
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        return f"{days // 7}w ago"
    return f"{days // 30}mo ago"


@router.get("/")
async def get_jobs(min_score: int = 1, limit: int = 100, user=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        user_id = int(user["user_id"])
        apps = await conn.fetch(
            """
            SELECT job_id, score, status, applied_at
            FROM applications
            WHERE user_id = $1 AND score >= $2
            ORDER BY score DESC
            LIMIT $3
        """,
            user_id,
            min_score,
            limit,
        )

        if not apps:
            return []

        job_ids = [a["job_id"] for a in apps]
        jobs = await conn.fetch(
            """
            SELECT id, title, company, location, url, source, description, created_at
            FROM jobs WHERE id = ANY($1)
        """,
            job_ids,
        )

        jobs_map = {j["id"]: dict(j) for j in jobs}
        result = []
        for app in apps:
            job = jobs_map.get(app["job_id"])
            if job:
                job["score"] = app["score"]
                job["status"] = app["status"]
                job["applied_at"] = str(app["applied_at"]) if app["applied_at"] else None
                job["experience_level"] = detect_experience_level(
                    job.get("title", ""), job.get("description", "")
                )
                job["posted_at"] = time_ago(job.get("created_at"))
                job.pop("created_at", None)
                job.pop("description", None)
                result.append(job)

        return result


@router.get("/stats")
async def get_stats(user=Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM jobs")
        scored = await conn.fetchval(
            "SELECT COUNT(*) FROM applications WHERE user_id = $1", user["user_id"]
        )
        strong = await conn.fetchval(
            "SELECT COUNT(*) FROM applications WHERE user_id = $1 AND score >= 7",
            user["user_id"],
        )
        applied = await conn.fetchval(
            "SELECT COUNT(*) FROM applications WHERE user_id = $1 AND status = 'applied'",
            user["user_id"],
        )
        return {
            "total_jobs": total,
            "scored": scored,
            "strong_matches": strong,
            "applied": applied,
        }
