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
from scrapers.dice import scrape_dice
from scrapers.ycombinator import scrape_ycombinator
from scrapers.wellfound import scrape_wellfound
from scrapers.jsearch import scrape_jsearch
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
        ("Dice",                scrape_dice),
        ("Y Combinator",        scrape_ycombinator),
        ("Wellfound",           scrape_wellfound),
        ("LinkedIn/Indeed",     scrape_jsearch),
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

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
