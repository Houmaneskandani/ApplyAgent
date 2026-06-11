import asyncio
import json

DAILY_LIMIT = 10
MIN_SCORE = 6  # bot won't auto-apply to anything weaker than this

# asyncio.create_task returns a task the loop only holds a WEAK reference to —
# if we don't keep our own reference it can be garbage-collected mid-flight
# (and silently never finish). Keep a strong ref until the task completes.
_bg_tasks: set = set()


def _spawn(coro):
    """Fire-and-forget a coroutine while keeping a strong reference."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


def _job_passes_saved_filters(job: dict, filters: dict) -> bool:
    """
    Decide whether a candidate job survives the user's SAVED FilterPanel
    state. Mirrors the saved-filter portion of Dashboard.jsx::filteredJobs
    so what the user sees in their browse view is exactly what Auto Apply
    targets — no surprises.

    Quick toggles on the dashboard (Strong-8+, Remote Only, Past 7d, etc)
    are deliberately NOT considered here: those are ephemeral browsing
    state, not "I want to apply to these for me at 3am" intent.
    """
    title = (job.get("title") or "").lower()
    company = (job.get("company") or "").lower()
    location = (job.get("location") or "").lower()
    description = (job.get("description") or "").lower()

    # 1. Keywords — at least ONE must appear somewhere in title/company/description
    kw = filters.get("keywords") or []
    if kw:
        hay = f"{title} {company} {description}"
        if not any(k.lower() in hay for k in kw):
            return False

    # 2. Experience level — mirror the EXP_MAP from Dashboard
    exp_in = filters.get("experience") or []
    if exp_in:
        from api.routes.jobs import detect_experience_level
        EXP_MAP = {
            "Entry Level & Graduate":       "Entry",
            "Junior (1-2 years)":           "Junior",
            "Mid Level (3-5 years)":        "Mid Level",
            "Senior (5-8 years)":           "Senior",
            "Staff / Principal (8+ years)": "Staff / Principal",
        }
        wanted = {EXP_MAP.get(e) for e in exp_in if EXP_MAP.get(e)}
        if wanted:
            job_exp = detect_experience_level(title, description)
            if job_exp not in wanted:
                return False

    # 3. Work arrangement — Remote / Hybrid / Onsite
    wt = filters.get("work_type") or []
    if wt:
        from api.routes.jobs import detect_work_arrangement
        arr = detect_work_arrangement(title, location, description)
        wanted = {"Onsite" if w == "In person" else w for w in wt}
        if arr not in wanted:
            return False

    # 4. Industries — at least one industry keyword in title/company/description
    inds = filters.get("industries") or []
    if inds:
        hay = f"{title} {company} {description}"
        if not any(i.lower() in hay for i in inds):
            return False

    # 5. Location text input
    loc_filter = (filters.get("location") or "").strip().lower()
    if loc_filter:
        if loc_filter not in location:
            if loc_filter == "remote":
                from api.routes.jobs import detect_work_arrangement
                if detect_work_arrangement(title, location, description) != "Remote":
                    return False
            else:
                return False

    # 6. Excluded companies — comma-separated substring match on company name
    exc = filters.get("exclude_companies") or ""
    if exc:
        excluded = [e.strip().lower() for e in exc.split(",") if e.strip()]
        if any(e in company for e in excluded):
            return False

    # 7. Minimum salary — coarse search for "$NNNk" patterns. We KEEP jobs
    # with no salary mention (over-filtering hurts more than under-filtering
    # here since the AI matcher already scored the job above MIN_SCORE).
    min_sal = filters.get("min_salary") or 0
    if min_sal > 0:
        import re as _re_sal
        matches = _re_sal.findall(r"\$?\s?(\d{2,3})[,\s]?(\d{3})?\s?k?", description)
        salaries: list[int] = []
        for m in matches:
            try:
                n = int((m[0] or "") + (m[1] or ""))
                if n < 1000:
                    n *= 1000
                if 30_000 <= n <= 800_000:
                    salaries.append(n)
            except ValueError:
                pass
        if salaries and max(salaries) < min_sal * 1000:
            return False

    return True


async def auto_apply_for_user(user_id: int) -> int:
    """
    Queue top-scored new jobs for a user — RESPECTING their saved
    Dashboard filters (FilterPanel state). Returns jobs queued.

    Strategy:
      1. Check daily/queue limits.
      2. Read user.preferences.dashboard_filters.
      3. SQL-fetch a wide pool of top-scored 'new' candidates.
      4. Walk the pool in score-DESC order; pick the first N that pass
         the saved filters.
    """
    from db import get_pool, add_to_queue
    from api.routes.queue import process_user_queue
    pool = await get_pool()

    async def _decide_and_queue() -> int:
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

            user_row = await conn.fetchrow(
                "SELECT preferences FROM users WHERE id = $1", user_id,
            )
            prefs_raw = user_row["preferences"] if user_row else {}
            if isinstance(prefs_raw, str):
                try:
                    prefs_raw = json.loads(prefs_raw)
                except Exception:
                    prefs_raw = {}
            filters = (prefs_raw or {}).get("dashboard_filters") or {}

            pool_size = max(remaining * 5, 30)
            candidates = await conn.fetch("""
                SELECT a.job_id, a.score,
                       j.title, j.company, j.location, j.description
                  FROM applications a
                  JOIN jobs j ON j.id = a.job_id
                 WHERE a.user_id = $1 AND a.status = 'new' AND a.score >= $2
                 ORDER BY a.score DESC
                 LIMIT $3
            """, user_id, MIN_SCORE, pool_size)

        if not candidates:
            print(f"  [AutoApply] User {user_id}: no qualifying new jobs (score >= {MIN_SCORE}, status = new)")
            return 0

        matching: list = []
        for job in candidates:
            if _job_passes_saved_filters(dict(job), filters):
                matching.append(job)
                if len(matching) >= remaining:
                    break

        if not matching:
            active_filter_keys = [k for k, v in filters.items() if v and v != [] and v != ""]
            print(
                f"  [AutoApply] User {user_id}: {len(candidates)} candidates "
                f"in pool, 0 passed saved filters {active_filter_keys}"
            )
            return 0

        filter_note = (
            f" (filtered to {len(matching)} from {len(candidates)} candidates)"
            if filters else ""
        )
        print(
            f"  [AutoApply] User {user_id}: queuing {len(matching)} job(s)"
            f"{filter_note} — {applied_today} applied today, {remaining} remaining"
        )
        for job in matching:
            await add_to_queue(user_id, job["job_id"], dry_run=False)
        return len(matching)

    try:
        return await _decide_and_queue()
    finally:
        # ALWAYS kick the queue drainer for this user — on EVERY exit path
        # (limit reached, no candidates, no matches, success, OR exception).
        # This drains both rows we just queued AND any stranded in 'queued'
        # from a prior crash/early-return. Without it, a stopped queue never
        # restarts until a manual /apply click (the bug that froze the queue
        # for ~47h). `process_user_queue` is a no-op if already draining.
        _spawn(process_user_queue(user_id))


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
    from scrapers.ziprecruiter import scrape_ziprecruiter
    from db import get_pool

    # Resolve which job categories to search + keep this cycle: the union of
    # every user's preferences.job_categories. Drives the query-based scrapers
    # AND matcher.is_engineering_job's title filter. Defaults to software
    # engineering when nobody has configured categories.
    try:
        import job_categories as _jc
        pool = await get_pool()
        async with pool.acquire() as conn:
            _rows = await conn.fetch("SELECT preferences FROM users")
        _keys = set()
        for _r in _rows:
            _p = _r["preferences"]
            if isinstance(_p, str):
                try:
                    _p = json.loads(_p)
                except Exception:
                    _p = {}
            for _k in (_p or {}).get("job_categories", []) or []:
                _keys.add(_k)
        _jc.set_active(list(_keys))
        print(f"[Scraper] Active job categories: {_jc.active_keys()}")
    except Exception as _e:
        print(f"[Scraper] category resolve failed (using default): {type(_e).__name__}: {_e}")

    scrapers = [
        ("Greenhouse",   scrape_greenhouse),
        ("Lever",        scrape_lever),
        ("Himalayas",    scrape_himalayas),
        ("Remotive",     scrape_remotive),
        ("Dice",         scrape_dice),
        ("YCombinator",  scrape_ycombinator),
        ("Wellfound",    scrape_wellfound),
        ("JSearch",      scrape_jsearch),
        ("ZipRecruiter", scrape_ziprecruiter),
    ]

    async def run_one(name, fn):
        try:
            count = await fn()
            print(f"  [Scraper] {name}: {count} jobs")
            return count
        except Exception as e:
            print(f"  [Scraper] {name} failed: {e}")
            return 0

    print("\n[Scraper] Starting scrape (all sources in parallel)...")
    results = await asyncio.gather(*[run_one(n, f) for n, f in scrapers])
    print(f"[Scraper] Done — {sum(results)} total jobs scraped.")

    # Score the freshly-scraped jobs for every user, otherwise they keep
    # score=NULL and can never satisfy the auto-apply score>=6 bar. (This was
    # the missing step that made scheduler-scraped jobs un-auto-appliable.)
    from matcher import score_jobs
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT id FROM users")
    print(f"[Scraper] Scoring for {len(users)} user(s)...")
    for u in users:
        try:
            await score_jobs(u["id"])
        except Exception as e:
            print(f"  [Scraper] scoring user {u['id']} failed: {type(e).__name__}: {e}")


async def auto_apply_loop():
    """
    Long-running auto-apply + queue-drain loop for the API service.

    Decoupled from scrape+score (the cron worker does that), so it runs
    cheaply in the web process even when RUN_SCHEDULER_IN_WEB is off — this is
    what actually drains the queue and submits applications. Also recovers any
    rows stranded in 'queued' by a prior restart, on boot.
    """
    print("[AutoApplyLoop] Started")
    from db import get_pool
    from api.routes.queue import process_user_queue
    # Boot recovery: kick a drainer for every user with leftover 'queued' rows.
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT user_id FROM applications WHERE status = 'queued'"
            )
        for r in rows:
            _spawn(process_user_queue(r["user_id"]))
        if rows:
            print(f"[AutoApplyLoop] Boot: drained {len(rows)} user(s) with stranded queue rows")
    except Exception as e:
        print(f"[AutoApplyLoop] boot drain failed: {type(e).__name__}: {e}")
    # Periodic: queue fresh matches + drain. 20 min keeps it responsive without
    # hammering the DB.
    while True:
        await asyncio.sleep(1200)
        try:
            await run_auto_apply()
        except Exception as e:
            print(f"[AutoApplyLoop] error: {type(e).__name__}: {e}")


async def scheduler_loop():
    """Background task — scrapes every 6h, auto-applies every 1h. No scoring on startup."""
    print("[Scheduler] Started")
    scrape_counter = 0
    while True:
        await asyncio.sleep(3600)
        await run_auto_apply()
        scrape_counter += 1
        if scrape_counter % 6 == 0:
            _spawn(run_scrape_and_score())
