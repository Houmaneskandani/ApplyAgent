import asyncio
from fastapi import APIRouter, Depends, HTTPException
from api.auth import get_current_user
from db import get_pool
from applier.browser_utils import throttle_for_url

router = APIRouter()

# Max seconds a single application can run before being force-killed.
# Long-form Greenhouse jobs (Robinhood/Stripe/Pinterest) routinely have
# 15-25 custom questions, plus email verification waits up to 90s, plus
# 2-3 CAPTCHA detection rounds. 5 min was too tight; 10 min gives the
# longest jobs room to finish but still kills genuinely-stuck runs.
APPLICATION_TIMEOUT = 600  # 10 minutes

# Per-user serialization lock. When the user clicks "Apply" on multiple
# jobs in succession, each POST /apply/:id spawns its own background
# task that calls process_user_queue(user_id). Without this lock, all of
# those tasks would run in parallel, each pulling a different queued row
# via FOR UPDATE SKIP LOCKED — that's technically safe (no double-billing)
# but stresses CPU/RAM, hits Anthropic rate limits, makes the UI confusing,
# and burns more Gmail IMAP logins per minute than Google likes.
#
# With this lock: the FIRST background task takes the lock and drains the
# queue in order. Subsequent tasks see the lock is held and return
# immediately. The user's queued jobs run STRICTLY ONE AT A TIME, in
# queue-position order. Resource usage stays predictable.
#
# The FOR UPDATE SKIP LOCKED inside is kept as a DB-level safety net for
# the day we scale to multiple uvicorn workers — the lock here only
# serializes WITHIN a single process; the SQL serializes ACROSS processes.
_user_locks: dict[int, asyncio.Lock] = {}


def _get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


async def process_user_queue(user_id: int):
    """
    Drain the queued applications for a user, one at a time.

    Concurrency model:
      - PROCESS-LOCAL: per-user asyncio.Lock makes this strictly serial
        within a single uvicorn worker (the normal case).
      - DB-LEVEL: SELECT ... FOR UPDATE SKIP LOCKED makes the row claim
        atomic across multiple workers (future-proofing for horizontal
        scale).
      - PER-DOMAIN (in throttle_for_url): max 2 in-flight applies per ATS
        host, with 15-60s jitter. Defense against rate-limiting at the
        target site.
    """
    lock = _get_user_lock(user_id)
    if lock.locked():
        # Another background task is already draining this user's queue.
        # Bail out — they'll pick up whatever we would have processed.
        return

    async with lock:
        # Drain the queue serially. New rows added while we're processing
        # also get picked up here (the SELECT runs every iteration).
        while True:
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Atomic claim: SELECT a single queued row with FOR UPDATE
                # SKIP LOCKED inside an UPDATE, returning the claimed columns
                # in one round trip. Concurrent callers (other workers) see
                # different rows — this is the multi-worker safety net.
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
                    break  # queue is empty — done

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
                # Per-domain throttle: still enforced even though we're
                # serial here — if a user only ever applies to Greenhouse
                # jobs, the throttle's 15-60s jitter between same-domain
                # applies kicks in between iterations.
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
                        UPDATE applications SET status = 'failed', notes = 'Timed out after 10 minutes'
                        WHERE user_id = $1 AND job_id = $2
                    """, user_id, row["job_id"])
            except Exception as e:
                print(f"  ✗ Queue processor error: {e}")
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE applications SET status = 'failed', notes = $3
                        WHERE user_id = $1 AND job_id = $2
                    """, user_id, row["job_id"], f"Error: {e}"[:500])


@router.get("/")
async def get_queue(user=Depends(get_current_user)):
    """Get the current queue (queued + applying) for the logged-in user."""
    user_id = user["user_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Auto-reset any jobs stuck in 'applying' for more than 15 minutes.
        # This is the SAFETY NET — APPLICATION_TIMEOUT (10 min) should catch
        # everything first, but if the worker crashed mid-apply and never
        # updated status, this stops the row from staying 'applying' forever.
        # Use applied_at (set to NOW() when we started applying) not created_at.
        await conn.execute("""
            UPDATE applications
            SET status = 'failed', notes = 'Timed out — no response after 15 minutes'
            WHERE user_id = $1
              AND status = 'applying'
              AND applied_at < NOW() - INTERVAL '15 minutes'
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
