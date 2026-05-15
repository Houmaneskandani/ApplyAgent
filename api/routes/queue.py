import asyncio
from fastapi import APIRouter, Depends, HTTPException
from api.auth import get_current_user
from db import get_pool
from applier.browser_utils import throttle_for_url

router = APIRouter()

# Max seconds a single application can run before being force-killed
APPLICATION_TIMEOUT = 300  # 5 minutes


async def process_user_queue(user_id: int):
    """
    Process all queued jobs for a user one at a time.

    SAFE for multi-worker deployments: the row claim is a single atomic
    UPDATE against the DB using `SELECT ... FOR UPDATE SKIP LOCKED`, so
    if two FastAPI workers both run this function for the same user, they
    can't both grab the same row. Each gets a different queued row, or
    one gets nothing and exits.

    Previously this used a process-local asyncio.Lock dict (`_user_locks`),
    which only worked with a single worker. Scaling to N workers would
    have caused double-billing of the same application.
    """
    while True:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Atomic claim: SELECT a single queued row with FOR UPDATE
            # SKIP LOCKED inside an UPDATE, returning the claimed columns
            # in one round trip. Concurrent callers see different rows.
            row = await conn.fetchrow("""
                UPDATE applications
                   SET status = 'applying',
                       notes = 'Starting...',
                       applied_at = NOW()
                 WHERE id = (
                     SELECT id FROM applications
                      WHERE user_id = $1 AND status = 'queued'
                      ORDER BY queue_position ASC, created_at ASC
                      LIMIT 1
                      FOR UPDATE SKIP LOCKED
                 )
                RETURNING job_id, dry_run
            """, user_id)

            if not row:
                break

            job_meta = await conn.fetchrow("""
                SELECT j.title, j.company, j.location, j.url, j.source, j.description
                  FROM jobs j WHERE j.id = $1
            """, row["job_id"])
            if not job_meta:
                # Orphaned application row — mark failed and move on.
                await conn.execute(
                    "UPDATE applications SET status = 'failed', notes = 'Job no longer exists' "
                    "WHERE user_id = $1 AND job_id = $2",
                    user_id, row["job_id"],
                )
                continue

        # Lazy import to avoid circular dependency
        from api.routes.apply import run_application
        job_dict = {
            "id": row["job_id"],
            "title": job_meta["title"],
            "company": job_meta["company"],
            "location": job_meta["location"],
            "url": job_meta["url"],
            "source": job_meta["source"],
            "description": job_meta["description"],
        }
        try:
            # Per-domain throttle: no more than MAX_CONCURRENT_PER_DOMAIN (2)
            # in-flight applications to the same ATS host, with a 15-60s jitter
            # cooldown before the next one in the queue can grab the slot.
            # This is the single biggest behavioral change that drops the
            # "20 applies from one IP in 5 minutes" bot signature.
            async with throttle_for_url(job_meta["url"]):
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
