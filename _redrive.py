import asyncio
from db import get_pool, add_to_queue

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await conn.fetchval("SELECT id FROM users WHERE email=$1","eskandanihouman@gmail.com")
        rows = await conn.fetch("""
            SELECT DISTINCT ON (LOWER(COALESCE(j.company,'')), LOWER(j.title)) a.job_id, j.title, j.company
              FROM applications a JOIN jobs j ON j.id=a.job_id
             WHERE a.user_id=$1 AND a.dry_run=true AND COALESCE(a.status,'new')='new'
               AND a.applied_at > NOW() - INTERVAL '72 hours'
             ORDER BY LOWER(COALESCE(j.company,'')), LOWER(j.title), a.score DESC""", uid)
    print(f"re-queuing {len(rows)} jobs as LIVE...", flush=True)
    for r in rows:
        await add_to_queue(uid, r["job_id"], dry_run=False)
    print("queued. draining now (real applies, sequential)...", flush=True)
    from api.routes.queue import process_user_queue
    await process_user_queue(uid)
    async with pool.acquire() as conn:
        done = await conn.fetch("""
            SELECT status, COUNT(*) c FROM applications
            WHERE user_id=$1 AND dry_run=false AND applied_at > NOW() - INTERVAL '2 hours'
            GROUP BY 1""", uid)
    print("RESULT (live, last 2h):", {r['status']: r['c'] for r in done}, flush=True)

asyncio.run(main())
