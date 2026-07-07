"""
Scrape + score worker — run on a schedule (Railway cron, crontab, etc.)
Usage: python worker.py
Railway cron: set schedule to "0 */6 * * *" (every 6 hours)
"""
import asyncio
from db import init_db, get_pool
from scrapers.greenhouse import scrape_greenhouse
from scrapers.lever import scrape_lever
from scrapers.himalayas import scrape_himalayas
from scrapers.remotive import scrape_remotive
# dice/ycombinator/wellfound retired — APIs dead or bot-walled (0 results,
# log noise). Files kept in scrapers/ for future revival.
from scrapers.jsearch import scrape_jsearch
from scrapers.hackernews import scrape_hackernews
from scrapers.ziprecruiter import scrape_ziprecruiter
from matcher import score_jobs


async def main():
    print("=== Worker: scrape + score ===")
    await init_db()

    # Resolve which categories to search + the local area BEFORE scraping.
    # Without this the cron scraped with the software-only default no matter
    # what categories users picked (the resolve only lived in scheduler_loop,
    # which is off in the web service).
    try:
        import job_categories
        await job_categories.resolve_active_from_db()
    except Exception as e:
        print(f"  Category resolve failed (using default): {type(e).__name__}: {e}")

    print("\n── Scraping ──────────────────────────────")

    totals = {}
    for name, fn in [
        ("Greenhouse",          scrape_greenhouse),
        ("Lever",               scrape_lever),
        ("Himalayas",           scrape_himalayas),
        ("Remotive/Remote.co",  scrape_remotive),
        ("LinkedIn/Indeed",     scrape_jsearch),
        ("HN Who is hiring",    scrape_hackernews),
        ("ZipRecruiter",        scrape_ziprecruiter),
    ]:
        try:
            count = await fn()
            totals[name] = count
        except Exception as e:
            print(f"  {name} failed: {e}")
            totals[name] = 0

    total = sum(totals.values())
    print(f"\nTotal scraped: {total}")
    for name, count in totals.items():
        print(f"  {name}: {count}")

    # Stamp last_scraped_at on every user so the dashboard's "Last updated"
    # reflects reality. Only the in-web scheduler stamped it before, and that
    # path is off in production (RUN_SCHEDULER_IN_WEB=0) — so the UI showed
    # "Last updated: Never" forever, reading as "the bot is doing nothing".
    # Read-modify-write per user keeps every other pref key (incl. encrypted
    # secrets, which are stored encrypted in place) untouched.
    try:
        import json
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, preferences FROM users")
            for r in rows:
                p = r["preferences"]
                if isinstance(p, str):
                    try:
                        p = json.loads(p)
                    except Exception:
                        p = {}
                p = p or {}
                p["last_scraped_at"] = now_iso
                await conn.execute(
                    "UPDATE users SET preferences = $1 WHERE id = $2",
                    json.dumps(p), r["id"],
                )
        print(f"  Stamped last_scraped_at for {len(rows)} user(s)")
    except Exception as e:
        print(f"  last_scraped_at stamp failed: {type(e).__name__}: {e}")

    # Score for every user
    pool = await get_pool()
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT id, name FROM users")

    print(f"\n── Scoring for {len(users)} user(s) ──────────")
    for user in users:
        print(f"\n  User {user['id']} ({user['name']}):")
        try:
            await score_jobs(user["id"])
        except Exception as e:
            print(f"    Error scoring user {user['id']}: {e}")

    # Daily digest — at most ONE email per user per day (the cron runs 4x/day;
    # prefs.digest_last_sent gates it) with the strongest matches scraped in
    # the last 24h. No matches = no email (never send an empty digest).
    try:
        from notifications import _send_email
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        today = _dt.now(_tz.utc).date().isoformat()
        async with pool.acquire() as conn:
            urows = await conn.fetch(
                "SELECT id, email, name, preferences FROM users WHERE COALESCE(email,'') <> ''")
            for u in urows:
                p = u["preferences"]
                if isinstance(p, str):
                    try:
                        p = _json.loads(p)
                    except Exception:
                        p = {}
                p = p or {}
                if p.get("digest_last_sent") == today:
                    continue
                top = await conn.fetch("""
                    SELECT a.score, j.title, j.company, j.location
                      FROM applications a JOIN jobs j ON j.id = a.job_id
                     WHERE a.user_id = $1 AND a.score >= 7
                       AND COALESCE(a.status, 'new') = 'new'
                       AND j.created_at > NOW() - INTERVAL '24 hours'
                     ORDER BY a.score DESC LIMIT 8""", u["id"])
                if not top:
                    continue
                lines = "\n".join(
                    f"  [{r['score']}/10] {r['title']} @ {r['company']}"
                    + (f"  ({r['location']})" if r["location"] else "")
                    for r in top)
                first = (u["name"] or "there").split(" ")[0]
                _send_email(
                    f"☀️ {len(top)} strong new match{'es' if len(top) != 1 else ''} today",
                    f"Hi {first},\n\nFresh postings from the last 24 hours that "
                    f"scored 7+ against your profile:\n\n{lines}\n\n"
                    f"Review and apply: https://apply-agent-frontend.vercel.app/dashboard\n\n"
                    f"— ApplyAgent",
                    u["email"])
                p["digest_last_sent"] = today
                await conn.execute(
                    "UPDATE users SET preferences = $1 WHERE id = $2",
                    _json.dumps(p), u["id"])
                print(f"  ☀️ Digest sent to user {u['id']} ({len(top)} matches)")
    except Exception as e:
        print(f"  Digest failed: {type(e).__name__}: {e}")

    # Prune long-expired postings nobody touched. Runs here (the 6h cron), not
    # the always-on API loop, since it's a once-per-cycle housekeeping step.
    try:
        from db import prune_stale_jobs
        pruned = await prune_stale_jobs()
        if pruned:
            print(f"\n── Pruned {pruned} stale job(s) (no applications, past retention) ──")
    except Exception as e:
        print(f"    Prune failed: {type(e).__name__}: {e}")

    # NOTE: this worker is a SHORT-LIVED cron (it exits when main() returns),
    # so it deliberately does NOT run auto-apply — Playwright applies take
    # minutes and would be killed on exit. Queueing + draining happens in the
    # long-running API service (scheduler.auto_apply_loop), which picks up the
    # freshly-scored jobs within ~20 min.
    print("\n=== Done (scrape + score). Auto-apply runs in the API service. ===")


if __name__ == "__main__":
    asyncio.run(main())
