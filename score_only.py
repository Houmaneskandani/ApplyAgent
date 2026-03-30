import asyncio
from db import get_or_create_user
from matcher import score_jobs
from config import USER_EMAIL, USER_NAME

async def main():
    user_id = await get_or_create_user(USER_EMAIL, USER_NAME)
    print(f"Scoring for user {user_id}...")
    await score_jobs(user_id, rescore=True)
    print("Done!")

asyncio.run(main())
