import asyncio
from db import init_db, get_or_create_user
from scrapers.greenhouse import scrape_greenhouse
from scrapers.lever import scrape_lever
from matcher import score_jobs

MY_EMAIL = "eskandanihouman@gmail.com"
MY_NAME = "Houman"

async def main():
    print("Initializing database...")
    await init_db()

    print("\nCreating user profile...")
    user_id = await get_or_create_user(MY_EMAIL, MY_NAME)
    print(f"  User ID: {user_id}")

    print("\nScraping Greenhouse jobs...")
    found = await scrape_greenhouse()
    print(f"  Found {found} jobs")

    print("\nScraping Lever jobs...")
    found = await scrape_lever()
    print(f"  Found {found} jobs")

    print("\nRunning matcher...")
    await score_jobs(user_id)

    print("\nDone. Run viewer.py to see results.")

if __name__ == "__main__":
    asyncio.run(main())
