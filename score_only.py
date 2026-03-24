import asyncio
from db import get_or_create_user
from matcher import score_jobs

async def main():
    user_id = await get_or_create_user("eskandanihouman@gmail.com", "Houman")
    print(f"Scoring for user {user_id}...")
    await score_jobs(user_id)
    print("Done!")

asyncio.run(main())
