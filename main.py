import asyncio
from db import init_db, get_or_create_user
from scrapers.greenhouse import scrape_greenhouse
from scrapers.lever import scrape_lever
from scrapers.himalayas import scrape_himalayas
from scrapers.remotive import scrape_remotive
from scrapers.dice import scrape_dice
from scrapers.ycombinator import scrape_ycombinator
from scrapers.wellfound import scrape_wellfound
from scrapers.jsearch import scrape_jsearch
from matcher import score_jobs
from config import USER_EMAIL, USER_NAME


async def main():
    print("Initializing database...")
    await init_db()

    print("\nCreating user profile...")
    user_id = await get_or_create_user(USER_EMAIL, USER_NAME)
    print(f"  User ID: {user_id}")

    print("\n── Scraping jobs ──────────────────────────")

    print("\n[Greenhouse]")
    await scrape_greenhouse()

    print("\n[Lever]")
    await scrape_lever()

    print("\n[Himalayas]")
    await scrape_himalayas()

    print("\n[Remotive / Remote.co]")
    await scrape_remotive()

    print("\n[Dice]")
    await scrape_dice()

    print("\n[Y Combinator / Work at a Startup]")
    await scrape_ycombinator()

    print("\n[Wellfound / AngelList]")
    await scrape_wellfound()

    print("\n[LinkedIn + Indeed via JSearch]")
    await scrape_jsearch()

    print("\n── Scoring matches ────────────────────────")
    await score_jobs(user_id)

    print("\nDone. Run viewer.py to see results.")


if __name__ == "__main__":
    asyncio.run(main())
