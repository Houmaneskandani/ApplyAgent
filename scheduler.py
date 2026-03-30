import asyncio

DAILY_LIMIT = 10


async def auto_apply_for_user(user_id: int) -> int:
    """Queue top-scored new jobs for a user, up to their daily limit. Returns jobs queued."""
    from db import get_pool, add_to_queue
    pool = await get_pool()
    async with pool.acquire() as conn:
        applied_today = await conn.fetchval("""
            SELECT COUNT(*) FROM applications
            WHERE user_id = $1 AND status = 'applied' AND dry_run = false
            AND applied_at >= CURRENT_DATE
        """, user_id)

        in_progress = await conn.fetchval("""
            SELECT COUNT(*) FROM applications
            WHERE user_id = $1 AND status IN ('queued', 'applying')
        """, user_id)

        remaining = DAILY_LIMIT - int(applied_today or 0) - int(in_progress or 0)
        if remaining <= 0:
            print(f"  [AutoApply] User {user_id}: daily limit reached "
                  f"({applied_today}/{DAILY_LIMIT} applied, {in_progress} in progress)")
            return 0

        jobs = await conn.fetch("""
            SELECT a.job_id FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE a.user_id = $1 AND a.status = 'new' AND a.score >= 6
            ORDER BY a.score DESC
            LIMIT $2
        """, user_id, remaining)

        if not jobs:
            print(f"  [AutoApply] User {user_id}: no qualifying new jobs (score >= 6, status = new)")
            return 0

        print(f"  [AutoApply] User {user_id}: queuing {len(jobs)} job(s) "
              f"({applied_today} applied today, {remaining} remaining)")
        for job in jobs:
            await add_to_queue(user_id, job["job_id"], dry_run=False)

    from api.routes.queue import process_user_queue
    asyncio.create_task(process_user_queue(user_id))
    return len(jobs)


async def run_auto_apply():
    """Run auto-apply for every user who has it enabled."""
    from db import get_pool
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            users = await conn.fetch("""
                SELECT id FROM users
                WHERE (preferences->>'auto_apply')::boolean = true
            """)
        if not users:
            return
        print(f"\n[AutoApply] Running for {len(users)} user(s)...")
        for user in users:
            try:
                await auto_apply_for_user(user["id"])
            except Exception as e:
                print(f"  [AutoApply] Error for user {user['id']}: {e}")
    except Exception as e:
        print(f"[AutoApply] Scheduler error: {e}")


async def run_scrape_and_score():
    """Scrape all sources and score jobs for every user."""
    from scrapers.greenhouse import scrape_greenhouse
    from scrapers.lever import scrape_lever
    from scrapers.himalayas import scrape_himalayas
    from scrapers.remotive import scrape_remotive
    from scrapers.dice import scrape_dice
    from scrapers.ycombinator import scrape_ycombinator
    from scrapers.wellfound import scrape_wellfound
    from scrapers.jsearch import scrape_jsearch
    from db import get_pool

    print("\n[Scraper] Starting scrape...")
    for name, fn in [
        ("Greenhouse", scrape_greenhouse),
        ("Lever", scrape_lever),
        ("Himalayas", scrape_himalayas),
        ("Remotive", scrape_remotive),
        ("Dice", scrape_dice),
        ("YCombinator", scrape_ycombinator),
        ("Wellfound", scrape_wellfound),
        ("JSearch", scrape_jsearch),
    ]:
        try:
            count = await fn()
            print(f"  [Scraper] {name}: {count} jobs")
        except Exception as e:
            print(f"  [Scraper] {name} failed: {e}")

    print("[Scraper] Scrape done. (Scoring runs on-demand via Search Jobs button.)")


async def scheduler_loop():
    """Background task — scrapes every 6h, auto-applies every 1h. No scoring on startup."""
    print("[Scheduler] Started")
    scrape_counter = 0
    while True:
        await asyncio.sleep(3600)
        await run_auto_apply()
        scrape_counter += 1
        if scrape_counter % 6 == 0:
            asyncio.create_task(run_scrape_and_score())
