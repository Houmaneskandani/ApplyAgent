import asyncio
from db import get_pool, add_to_queue

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await conn.fetchval("SELECT id FROM users WHERE email=$1","eskandanihouman@gmail.com")
    # DRY-RUN validation of the lever + ashby appliers on real postings.
    for jid in (1759463, 1847141):   # lever: Outreach | ashby: Deepgram
        await add_to_queue(uid, jid, dry_run=True)
    from api.routes.queue import process_user_queue
    await process_user_queue(uid)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT a.job_id, a.status, LEFT(COALESCE(a.notes,''),100) notes, j.title, j.url
              FROM applications a JOIN jobs j ON j.id=a.job_id
             WHERE a.user_id=$1 AND a.job_id = ANY($2)""", uid, [1759463, 1847141])
        for r in rows:
            ats = "lever" if "lever.co" in r["url"] else "ashby"
            print(f"VALIDATION [{ats}] {r['title'][:35]} -> status={r['status']} | {r['notes']}")
asyncio.run(main())
