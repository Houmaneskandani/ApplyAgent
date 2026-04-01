from fastapi import APIRouter, Depends, BackgroundTasks
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
                raw_dt = job.get("created_at")
                job["created_at"] = raw_dt.isoformat() if raw_dt else None
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
        credits = await conn.fetchval(
            "SELECT COALESCE(credits, 0) FROM users WHERE id = $1", user["user_id"]
        )
        # Use actual scrape timestamp stored on user, not MAX(created_at) from jobs
        # (MAX(created_at) never changes when scrapers find duplicate URLs)
        import json as _json
        prefs_row = await conn.fetchrow(
            "SELECT preferences FROM users WHERE id = $1", user["user_id"]
        )
        prefs = prefs_row["preferences"] or {}
        if isinstance(prefs, str):
            prefs = _json.loads(prefs)
        last_scraped_str = prefs.get("last_scraped_at")

        from datetime import datetime, timezone
        last_scraped = None
        if last_scraped_str:
            try:
                last_scraped = datetime.fromisoformat(last_scraped_str).replace(tzinfo=timezone.utc)
            except Exception:
                pass

        return {
            "total_jobs": total,
            "scored": scored,
            "strong_matches": strong,
            "applied": applied,
            "credits": round(float(credits or 0), 1),
            "last_scraped": last_scraped_str,
            "last_scraped_ago": time_ago(last_scraped) if last_scraped else "Never",
        }


@router.post("/scrape")
async def trigger_scrape(background_tasks: BackgroundTasks, user=Depends(get_current_user)):
    """Kick off a full scrape + rescore in the background."""
    user_id = user["user_id"]

    async def _run():
        from scrapers.greenhouse import scrape_greenhouse
        from scrapers.lever import scrape_lever
        from scrapers.himalayas import scrape_himalayas
        from scrapers.remotive import scrape_remotive
        from scrapers.dice import scrape_dice
        from scrapers.ycombinator import scrape_ycombinator
        from scrapers.wellfound import scrape_wellfound
        from scrapers.jsearch import scrape_jsearch
        from matcher import score_jobs
        from db import get_pool
        import asyncio, json as _json
        from datetime import datetime, timezone

        print(f"\n=== Manual scrape triggered by user {user_id} ===")

        # Count jobs before scraping to report how many new ones were found
        pool = await get_pool()
        async with pool.acquire() as conn:
            before_count = await conn.fetchval("SELECT COUNT(*) FROM jobs")

        async def _safe(name, fn):
            try:
                print(f"  [{name}] starting...")
                await fn()
                print(f"  [{name}] done")
            except Exception as e:
                print(f"  [{name}] error: {e}")

        # Run all scrapers in parallel
        await asyncio.gather(
            _safe("Greenhouse",     scrape_greenhouse),
            _safe("Lever",          scrape_lever),
            _safe("Himalayas",      scrape_himalayas),
            _safe("Remotive",       scrape_remotive),
            _safe("Dice",           scrape_dice),
            _safe("Y Combinator",   scrape_ycombinator),
            _safe("Wellfound",      scrape_wellfound),
            _safe("LinkedIn/Indeed",scrape_jsearch),
        )

        pool = await get_pool()
        async with pool.acquire() as conn:
            after_count = await conn.fetchval("SELECT COUNT(*) FROM jobs")
        new_jobs = (after_count or 0) - (before_count or 0)
        print(f"  📊 Scrape result: {new_jobs} new jobs found (total: {after_count})")

        print(f"  Scoring for user {user_id}...")
        await score_jobs(user_id)

        # Stamp actual scrape time on the user record so the dashboard shows it correctly
        now_iso = datetime.now(timezone.utc).isoformat()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT preferences FROM users WHERE id = $1", user_id)
            prefs = row["preferences"] or {}
            if isinstance(prefs, str):
                prefs = _json.loads(prefs)
            prefs["last_scraped_at"] = now_iso
            await conn.execute(
                "UPDATE users SET preferences = $1::jsonb WHERE id = $2",
                _json.dumps(prefs), user_id,
            )

        print(f"=== Scrape complete — {new_jobs} new jobs, scored, timestamp saved ===")

    background_tasks.add_task(_run)
    return {"status": "scraping", "message": "Scrape started in background — new jobs will appear in a few minutes"}


@router.get("/{job_id}")
async def get_job(job_id: int, user=Depends(get_current_user)):
    from fastapi import HTTPException
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, company, location, url, source, description, created_at FROM jobs WHERE id = $1",
            job_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        job = dict(row)
        app = await conn.fetchrow(
            "SELECT score, status FROM applications WHERE job_id = $1 AND user_id = $2",
            job_id, user["user_id"],
        )
        if app:
            job["score"] = app["score"]
            job["status"] = app["status"]
        job["experience_level"] = detect_experience_level(job.get("title", ""), job.get("description", ""))
        job["posted_at"] = time_ago(job.get("created_at"))
        job.pop("created_at", None)
        return job
