from fastapi import APIRouter, Depends, BackgroundTasks, Request, Response
from api.auth import get_current_user, _rate_limit
from db import get_pool

router = APIRouter()


def classify_ats(source: str | None, url: str | None) -> str:
    """
    Return the effective ATS bucket the apply-dispatcher will route to.
    Mirrors the routing logic in api/routes/apply.py:run_application
    EXACTLY (same source values + URL substrings). One source of truth
    so the dashboard filter, the per-ATS stats, and the actual apply
    dispatcher can never disagree on what counts as "Lever".
    """
    src = (source or "").lower()
    url_l = (url or "").lower()
    if src == "greenhouse" or "greenhouse.io" in url_l or "gh_jid" in url_l:
        return "greenhouse"
    if src == "lever" or "lever.co" in url_l:
        return "lever"
    if src == "ashby" or "ashby.io" in url_l or "ashbyhq.com" in url_l:
        return "ashby"
    if src == "smartrecruiters" or "smartrecruiters.com" in url_l:
        return "smartrecruiters"
    if src == "workday" or "myworkdayjobs.com" in url_l or "workday.com" in url_l:
        return "workday"
    if src == "ziprecruiter" or "ziprecruiter.com" in url_l:
        return "ziprecruiter"
    return "generic"


def detect_experience_level(title: str, description: str = "") -> str:
    """
    Heuristic seniority detection. Returns one of the values in the
    EXPERIENCE_LEVELS frontend filter so the filter actually matches.
    """
    text = f"{title} {description}".lower()
    if any(w in text for w in ["staff", "principal", "distinguished", "fellow"]):
        return "Staff / Principal"
    if any(w in text for w in ["senior", "sr.", "sr ", "lead", "manager"]):
        return "Senior"
    if any(w in text for w in ["intern", "new grad", "graduate", "entry"]):
        return "Entry"  # NOTE: previously returned "Junior" — broke the Entry filter
    if any(w in text for w in ["junior", "jr.", "jr ", "associate"]):
        return "Junior"
    if any(w in text for w in ["mid", "mid-level", "intermediate", "ii", "iii"]):
        return "Mid Level"
    return "Mid Level"  # default


def detect_work_arrangement(title: str, location: str = "", description: str = "") -> str:
    """
    Detect Remote / Hybrid / Onsite from title + location + description.

    Why this matters: the previous frontend filter inferred this from the
    location string alone — but most hybrid jobs say "San Francisco, CA"
    with no "hybrid" word, and most in-person jobs ALSO have no marker.
    Folding the description into the detection catches "Remote position",
    "hybrid 3 days in office", etc.
    """
    text = f"{title} {location} {description}".lower()
    # Order matters — check Hybrid before Remote so "hybrid (remote 2 days)"
    # is classified as Hybrid, not Remote.
    if any(w in text for w in ["hybrid", "in-office", "3 days in office", "2 days in office", "in office"]):
        return "Hybrid"
    if any(w in text for w in ["remote", "work from home", "wfh", "anywhere", "distributed"]):
        return "Remote"
    return "Onsite"


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
async def get_jobs(
    response: Response,
    min_score: int = 1,
    limit: int = 100,
    ats: str | None = None,
    search: str | None = None,
    location: str | None = None,
    category: str | None = None,
    sort: str = "score",
    posted_within_days: int | None = None,
    user=Depends(get_current_user),
):
    """
    List the user's top-scored jobs.

    Optional server-side filters — ALL applied BEFORE the score-DESC LIMIT so
    they search the user's WHOLE scored set, not just the top-N slice the
    dashboard would otherwise download and filter in the browser:
      • `ats`      — the dispatcher's effective applier (same CASE as
                     classify_ats). Solves "I can't find any Lever jobs because
                     Greenhouse dominates my top 200".
      • `location` — substring match on job location (e.g. "Seattle"). Without
                     this, a client-side Seattle filter only ever saw the ~8
                     Seattle jobs that happened to land in the global top-200;
                     in SQL the LIMIT now picks the top-200 SEATTLE jobs.
      • `search`   — substring across title / company / location.

    `location`/`search` match ONLY columns the frontend's client-side filters
    also see (title/company/location) — the client additionally matches the
    description snippet, i.e. it's a strict SUPERSET, so the client refinement
    pass never re-hides a row the server returned.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        user_id = int(user["user_id"])
        # SAFETY: validate `ats` against the known applier set so the
        # CASE-equality below never sees an attacker-controlled string.
        VALID_ATS = {"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "ziprecruiter", "generic"}
        ats_filter = ats if ats in VALID_ATS else None

        # Assemble the WHERE clause with positional params. EVERY value is
        # bound ($n) — never string-interpolated — so this stays injection-safe
        # exactly like the original ats parameterization.
        where = ["a.user_id = $1", "a.score >= $2"]
        args: list = [user_id, min_score]

        def _bind(val) -> str:
            args.append(val)
            return f"${len(args)}"

        loc = (location or "").strip()
        if loc:
            where.append(f"j.location ILIKE '%' || {_bind(loc)} || '%'")

        q = (search or "").strip()
        if q:
            p = _bind(q)
            where.append(
                f"(j.title ILIKE '%'||{p}||'%' "
                f"OR j.company ILIKE '%'||{p}||'%' "
                f"OR j.location ILIKE '%'||{p}||'%')"
            )

        # Mode toggle: 'professional' = career jobs (everything not discovered
        # by a local commodity category — includes all historical NULL rows);
        # a specific category key = only jobs that category's queries found.
        # Validated against the known key set so the SQL never sees free text.
        if category:
            import job_categories as _jc
            local = sorted(_jc.local_keys())
            if category == "professional" and local:
                p = _bind(local)
                where.append(f"(j.category IS NULL OR NOT (j.category = ANY({p})))")
            elif category in _jc.JOB_CATEGORIES:
                where.append(f"j.category = {_bind(category)}")

        # Freshness window, applied server-side so "Past 24h / 7 days" search
        # the WHOLE corpus (client-side they could only subtract from the
        # top-200-by-score, which is dominated by old jobs).
        if posted_within_days is not None:
            days = max(1, min(int(posted_within_days), 365))
            where.append(f"j.created_at >= NOW() - ({_bind(days)} || ' days')::interval")

        # Sort: whitelist-mapped (never interpolate user input). "date" makes
        # NEW jobs reachable — the all-time score ranking is dominated by old
        # 8-9s, so without a server-side newest sort a fresh job could NEVER
        # appear in the capped window. Score ties break newest-first.
        ORDER_SQL = {
            "score": "a.score DESC, j.created_at DESC",
            "date":  "j.created_at DESC, a.score DESC",
        }
        order_by = ORDER_SQL.get(sort, ORDER_SQL["score"])

        if ats_filter:
            # Same CASE classifier as classify_ats(), applied pre-LIMIT.
            where.append(
                "CASE "
                "WHEN j.source = 'greenhouse' OR j.url LIKE '%greenhouse.io%' OR j.url LIKE '%gh_jid%' THEN 'greenhouse' "
                "WHEN j.source = 'lever' OR j.url LIKE '%lever.co%' THEN 'lever' "
                "WHEN j.source = 'ashby' OR j.url LIKE '%ashby.io%' OR j.url LIKE '%ashbyhq.com%' THEN 'ashby' "
                "WHEN j.source = 'smartrecruiters' OR j.url LIKE '%smartrecruiters.com%' THEN 'smartrecruiters' "
                "WHEN j.source = 'workday' OR j.url LIKE '%myworkdayjobs.com%' OR j.url LIKE '%workday.com%' THEN 'workday' "
                "WHEN j.source = 'ziprecruiter' OR j.url LIKE '%ziprecruiter.com%' THEN 'ziprecruiter' "
                "ELSE 'generic' END = " + _bind(ats_filter)
            )

        # Always JOIN jobs (cheap PK join) so location/search/ats can filter
        # pre-LIMIT. The SELECTed columns are unchanged, so the response shape
        # is byte-identical to before. COUNT(*) OVER() = total matches before
        # the LIMIT — emitted via the X-Total-Count header ("N jobs found")
        # so the body stays a bare list for existing clients.
        limit_p = _bind(limit)
        apps = await conn.fetch(
            f"""
            SELECT a.job_id, a.score, a.status, a.applied_at, a.notes,
                   COUNT(*) OVER() AS _total
              FROM applications a
              JOIN jobs j ON j.id = a.job_id
             WHERE {' AND '.join(where)}
             ORDER BY {order_by}
             LIMIT {limit_p}
            """,
            *args,
        )
        response.headers["X-Total-Count"] = str(apps[0]["_total"] if apps else 0)

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
                job["notes"] = app["notes"]
                job["applied_at"] = str(app["applied_at"]) if app["applied_at"] else None
                desc = job.get("description", "") or ""
                job["experience_level"] = detect_experience_level(
                    job.get("title", ""), desc,
                )
                # New: explicit work arrangement so the frontend doesn't have to
                # guess from a substring match on location.
                job["work_arrangement"] = detect_work_arrangement(
                    job.get("title", ""), job.get("location", "") or "", desc,
                )
                job["posted_at"] = time_ago(job.get("created_at"))
                raw_dt = job.get("created_at")
                job["created_at"] = raw_dt.isoformat() if raw_dt else None
                # `ats` is the bucket the apply-dispatcher will route to.
                # NOT the same as `source` — e.g. a job scraped from
                # Indeed often redirects to a Greenhouse URL, so the
                # scrape source is "indeed" but the applier is "greenhouse".
                job["ats"] = classify_ats(job.get("source"), job.get("url"))
                # We strip the full description from the list response (it
                # bloats the payload + can be MBs of HTML). But we DO emit a
                # plain-text snippet so the frontend keyword filter can match
                # description text without re-fetching every job.
                if desc:
                    import re as _re
                    snippet = _re.sub(r"<[^>]+>", " ", desc)  # strip HTML tags
                    snippet = _re.sub(r"\s+", " ", snippet).strip()
                    job["description_snippet"] = snippet[:600].lower()
                else:
                    job["description_snippet"] = ""
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
        unknown = await conn.fetchval(
            "SELECT COUNT(*) FROM applications WHERE user_id = $1 AND status = 'unknown'",
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
            "unknown": unknown,
            "credits": round(float(credits or 0), 1),
            "last_scraped": last_scraped_str,
            "last_scraped_ago": time_ago(last_scraped) if last_scraped else "Never",
        }


@router.get("/stats/per-ats")
async def per_ats_stats(user=Depends(get_current_user)):
    """
    Aggregate apply outcomes by ATS (job source). Returns one row per
    source the user has at least one real attempt on, sorted by total
    attempts descending.

    Only counts LIVE applies (`dry_run = FALSE`) — dry runs would
    inflate the "applied" count without representing real submissions.
    Excludes `queued` / `applying` / `new` so we only aggregate
    terminal outcomes.

    Used by the Dashboard's "Apply performance by ATS" widget so the
    user can see which ATSes the bot is reliable on (high success rate)
    vs. which need investigation (lots of failed/unknown).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Group by the EFFECTIVE ATS the dispatcher routed to —
        # classify_ats(source, url) — NOT the raw j.source. A job tagged
        # source='ziprecruiter' but hosted on Greenhouse is applied via the
        # Greenhouse applier, so its outcome belongs in the greenhouse bucket.
        # classify_ats is Python substring logic, so we aggregate in Python.
        rows = await conn.fetch("""
            SELECT j.source AS source, j.url AS url, a.status AS status
              FROM applications a
              JOIN jobs j ON j.id = a.job_id
             WHERE a.user_id = $1
               AND a.dry_run = FALSE
               AND a.status IN ('applied', 'failed', 'unknown')
        """, user["user_id"])

    buckets: dict = {}
    for r in rows:
        ats = classify_ats(r["source"], r["url"]) or "generic"
        b = buckets.setdefault(
            ats, {"applied": 0, "failed": 0, "unknown": 0, "total": 0}
        )
        b["total"] += 1
        st = r["status"]
        if st in b:
            b[st] += 1

    out = []
    for ats, b in buckets.items():
        total = b["total"]
        applied = b["applied"]
        # Success rate = applied / total. Unknowns stay IN the denominator —
        # "tried but couldn't confirm" is a failed attempt until the user
        # manually confirms it.
        success_rate = round(100.0 * applied / total, 1) if total else None
        out.append({
            "source": ats,
            "applied": applied,
            "failed": b["failed"],
            "unknown": b["unknown"],
            "total": total,
            "success_rate_pct": success_rate,
        })
    out.sort(key=lambda x: (x["total"], x["applied"]), reverse=True)
    return {"per_ats": out, "total_attempts": sum(x["total"] for x in out)}


@router.get("/stats/by-ats-with-samples")
async def by_ats_with_samples(user=Depends(get_current_user)):
    """
    Per-ATS job counts + a shortlist of candidate jobs to test against.

    Mirrors the dispatcher logic in apply.py:run_application — same
    source/URL patterns — so the "ats" bucket returned here is exactly
    which applier code path will fire if the user clicks Apply on one
    of these jobs.

    For each ATS, returns up to 3 sample jobs ranked by:
      1. NOT yet applied / queued / unknown (skip already-attempted rows)
      2. Highest user-specific score (most worth applying to)
      3. Most recently scraped (posting most likely still open)

    Used by the dashboard's ATS validation flow: the user picks one
    sample per ATS, runs a dry-run on each, and checks the screenshot
    / logs to find ATS-specific bugs before doing a real apply.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            WITH classified AS (
                SELECT
                    j.id, j.title, j.company, j.url, j.source, j.created_at,
                    a.score, a.status,
                    CASE
                        WHEN j.source = 'greenhouse'
                          OR j.url LIKE '%greenhouse.io%'
                          OR j.url LIKE '%gh_jid%'
                            THEN 'greenhouse'
                        WHEN j.source = 'lever'
                          OR j.url LIKE '%lever.co%'
                            THEN 'lever'
                        WHEN j.source = 'ashby'
                          OR j.url LIKE '%ashby.io%'
                          OR j.url LIKE '%ashbyhq.com%'
                            THEN 'ashby'
                        WHEN j.source = 'smartrecruiters'
                          OR j.url LIKE '%smartrecruiters.com%'
                            THEN 'smartrecruiters'
                        WHEN j.source = 'workday'
                          OR j.url LIKE '%myworkdayjobs.com%'
                          OR j.url LIKE '%workday.com%'
                            THEN 'workday'
                        WHEN j.source = 'ziprecruiter'
                          OR j.url LIKE '%ziprecruiter.com%'
                            THEN 'ziprecruiter'
                        ELSE 'generic'
                    END AS ats
                  FROM jobs j
                  LEFT JOIN applications a
                    ON a.job_id = j.id AND a.user_id = $1
            ),
            ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY ats
                        ORDER BY
                            -- Untouched rows first; already-attempted last
                            CASE WHEN status IS NULL OR status = 'new' THEN 0 ELSE 1 END,
                            COALESCE(score, 0) DESC,
                            created_at DESC
                    ) AS rn,
                    COUNT(*) OVER (PARTITION BY ats) AS ats_total
                FROM classified
            )
            SELECT ats, ats_total, id, title, company, url, source, score, status
              FROM ranked
             WHERE rn <= 3
             ORDER BY ats_total DESC, ats, rn
        """, user["user_id"])

    # Re-shape: { ats: { total, samples: [...] } } keeping the
    # `ats_total DESC` ordering of the outer list so the most-stocked
    # ATSes appear first in the validation shortlist.
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        ats = r["ats"]
        if ats not in grouped:
            grouped[ats] = {
                "ats": ats,
                "total": int(r["ats_total"] or 0),
                "samples": [],
            }
            order.append(ats)
        grouped[ats]["samples"].append({
            "id": r["id"],
            "title": r["title"],
            "company": r["company"],
            "url": r["url"],
            "scrape_source": r["source"],
            "score": int(r["score"]) if r["score"] is not None else None,
            "status": r["status"] or "new",
        })

    return {"per_ats": [grouped[a] for a in order]}


@router.post("/scrape")
@_rate_limit("3/minute")
async def trigger_scrape(request: Request, background_tasks: BackgroundTasks, user=Depends(get_current_user)):
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
