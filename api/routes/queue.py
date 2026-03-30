import asyncio
from fastapi import APIRouter, Depends, HTTPException
from api.auth import get_current_user
from db import get_pool

router = APIRouter()

_user_locks: dict[int, asyncio.Lock] = {}

# Max seconds a single application can run before being force-killed
APPLICATION_TIMEOUT = 300  # 5 minutes


def _get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


async def process_user_queue(user_id: int):
    """Process all queued jobs for a user one at a time. Safe to call concurrently."""
    lock = _get_lock(user_id)
    if lock.locked():
        return  # Another processor is already running for this user

    async with lock:
        while True:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT a.job_id, a.dry_run,
                           j.title, j.company, j.location,
                           j.url, j.source, j.description
                    FROM applications a
                    JOIN jobs j ON j.id = a.job_id
                    WHERE a.user_id = $1 AND a.status = 'queued'
                    ORDER BY a.queue_position ASC, a.created_at ASC
                    LIMIT 1
                """, user_id)

                if not row:
                    break

                # Mark as applying before we release the DB connection
                # Also reset applied_at to NOW() so the 10-min cleanup uses a fresh timestamp
                # (created_at is from when the job was scored, possibly days ago)
                await conn.execute("""
                    UPDATE applications SET status = 'applying', notes = 'Starting...', applied_at = NOW()
                    WHERE user_id = $1 AND job_id = $2 AND status = 'queued'
                """, user_id, row["job_id"])

            # Lazy import to avoid circular dependency
            from api.routes.apply import run_application
            job_dict = {
                "id": row["job_id"],
                "title": row["title"],
                "company": row["company"],
                "location": row["location"],
                "url": row["url"],
                "source": row["source"],
                "description": row["description"],
            }
            try:
                await asyncio.wait_for(
                    run_application(job_dict, user_id, bool(row["dry_run"])),
                    timeout=APPLICATION_TIMEOUT,
                )
            except asyncio.TimeoutError:
                print(f"  ✗ Application timed out after {APPLICATION_TIMEOUT}s for job {row['job_id']}")
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE applications SET status = 'failed', notes = 'Timed out after 5 minutes'
                        WHERE user_id = $1 AND job_id = $2
                    """, user_id, row["job_id"])
            except Exception as e:
                print(f"  ✗ Queue processor error: {e}")
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE applications SET status = 'failed', notes = $3
                        WHERE user_id = $1 AND job_id = $2
                    """, user_id, row["job_id"], f"Error: {e}")


@router.get("/")
async def get_queue(user=Depends(get_current_user)):
    """Get the current queue (queued + applying) for the logged-in user."""
    user_id = user["user_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Auto-reset any jobs stuck in 'applying' for more than 10 minutes
        # Use applied_at (set to NOW() when we started applying) not created_at
        await conn.execute("""
            UPDATE applications
            SET status = 'failed', notes = 'Timed out — no response after 10 minutes'
            WHERE user_id = $1
              AND status = 'applying'
              AND applied_at < NOW() - INTERVAL '10 minutes'
        """, user_id)

        rows = await conn.fetch("""
            SELECT a.job_id, a.status, a.queue_position, a.dry_run, a.notes,
                   j.title, j.company, j.source
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE a.user_id = $1
              AND (
                a.status IN ('queued', 'applying')
                OR (a.status = 'failed' AND a.applied_at > NOW() - INTERVAL '1 hour')
              )
            ORDER BY
                CASE a.status
                    WHEN 'applying' THEN 0
                    WHEN 'queued'   THEN 1
                    WHEN 'failed'   THEN 2
                END,
                a.queue_position ASC,
                a.applied_at DESC
        """, user_id)
    return [dict(r) for r in rows]


@router.post("/trigger-scrape")
async def trigger_scrape(user=Depends(get_current_user)):
    """Manually trigger a scrape then score up to 100 new jobs for this user."""
    async def _run(user_id: int):
        from scheduler import run_scrape_and_score
        from matcher import score_jobs
        await run_scrape_and_score()
        print(f"[TriggerScrape] Scoring up to 100 new jobs for user {user_id}...")
        await score_jobs(user_id)
    asyncio.create_task(_run(user["user_id"]))
    return {"status": "scrape started — up to 100 new jobs will be scored for you"}


@router.delete("/{job_id}")
async def remove_from_queue(job_id: int, user=Depends(get_current_user)):
    """Cancel a queued job or dismiss a failed job (resets it to 'new' in Job Matches)."""
    user_id = user["user_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Cancel if queued
        result = await conn.execute("""
            UPDATE applications SET status = 'new', notes = NULL
            WHERE user_id = $1 AND job_id = $2 AND status = 'queued'
        """, user_id, job_id)
        if result != "UPDATE 0":
            return {"removed": True}
        # Dismiss if failed — reset to 'new' so it reappears in Job Matches
        result = await conn.execute("""
            UPDATE applications SET status = 'new', notes = NULL
            WHERE user_id = $1 AND job_id = $2 AND status = 'failed'
        """, user_id, job_id)
    if result == "UPDATE 0":
        raise HTTPException(
            status_code=400,
            detail="Cannot remove — job is currently being applied"
        )
    return {"removed": True}
