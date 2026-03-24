import asyncio
from db import get_top_jobs, get_or_create_user
from applier.greenhouse import apply_greenhouse
from applier.lever import apply_lever

MY_EMAIL = "eskandanihouman@gmail.com"
DRY_RUN = True  # ← flip to False when ready to actually apply


async def main():
    user_id = await get_or_create_user(MY_EMAIL, "Houman")
    jobs = await get_top_jobs(user_id, min_score=8, limit=10)

    print(f"Found {len(jobs)} top jobs to apply to")
    print(f"Mode: {'DRY RUN' if DRY_RUN else '🚀 LIVE'}\n")

    for job in jobs:
        url = job["url"] or ""
        source = job["source"] or ""

        if source == "greenhouse" or "greenhouse.io" in url or "gh_jid" in url:
            result = await apply_greenhouse(dict(job), dry_run=DRY_RUN)
        elif source == "lever" or "lever.co" in url:
            result = await apply_lever(dict(job), dry_run=DRY_RUN)
        else:
            print(f"  ⚠ Unknown source for {job['title']} — skipping")
            result = "skipped"

        print(f"  Result: {result}")
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
