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
from scrapers.ziprecruiter import scrape_ziprecruiter
from matcher import score_jobs


async def main():
    print("=== Worker: scrape + score ===")
    await init_db()

    print("\n── Scraping ──────────────────────────────")

    totals = {}
    for name, fn in [
        ("Greenhouse",          scrape_greenhouse),
        ("Lever",               scrape_lever),
        ("Himalayas",           scrape_himalayas),
        ("Remotive/Remote.co",  scrape_remotive),
        ("LinkedIn/Indeed",     scrape_jsearch),
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

    # NOTE: this worker is a SHORT-LIVED cron (it exits when main() returns),
    # so it deliberately does NOT run auto-apply — Playwright applies take
    # minutes and would be killed on exit. Queueing + draining happens in the
    # long-running API service (scheduler.auto_apply_loop), which picks up the
    # freshly-scored jobs within ~20 min.
    print("\n=== Done (scrape + score). Auto-apply runs in the API service. ===")


if __name__ == "__main__":
    asyncio.run(main())
