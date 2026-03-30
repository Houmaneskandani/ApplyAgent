"""
One-shot application test.
Usage:
  python test_apply_one.py            # dry run (takes screenshot, doesn't submit)
  python test_apply_one.py --live     # REAL submit — use with care
  python test_apply_one.py --job 123  # test a specific job id
"""
import asyncio
import sys
import os
import json
from db import init_db, get_pool
from api.routes.apply import run_application, build_profile_text, download_resume, extract_resume_text


async def pick_job(conn, job_id=None, source_filter=None):
    """Find a suitable test job from the database."""
    if job_id:
        row = await conn.fetchrow("""
            SELECT j.*, a.score FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id
            WHERE j.id = $1
            LIMIT 1
        """, job_id)
        return row

    # Prefer Greenhouse (most reliable), then Lever
    sources = [source_filter] if source_filter else ["greenhouse", "lever"]
    for src in sources:
        row = await conn.fetchrow("""
            SELECT j.*, a.score FROM jobs j
            JOIN applications a ON a.job_id = j.id
            WHERE j.source = $1 AND a.score >= 6 AND a.status = 'new'
            ORDER BY a.score DESC
            LIMIT 1
        """, src)
        if row:
            return row
    return None


async def main():
    dry_run = "--live" not in sys.argv
    specific_job_id = None
    if "--job" in sys.argv:
        idx = sys.argv.index("--job")
        specific_job_id = int(sys.argv[idx + 1])

    print("=== Application Test ===")
    print(f"Mode: {'DRY RUN (no submit)' if dry_run else '*** LIVE SUBMIT ***'}\n")

    await init_db()

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Get first user (for testing)
        user_row = await conn.fetchrow(
            "SELECT id, name, email, resume_url, preferences FROM users ORDER BY id LIMIT 1"
        )
        if not user_row:
            print("✗ No users found in database. Run main.py first.")
            return

        user_id = user_row["id"]
        print(f"Testing with user: {user_row['name']} ({user_row['email']}) — ID={user_id}")

        if not user_row["resume_url"]:
            print("✗ User has no resume_url set. Upload a resume via the web app first.")
            return

        job_row = await pick_job(conn, job_id=specific_job_id)
        if not job_row:
            print("✗ No suitable jobs found (need score>=6 and status='new').")
            print("  Run the scraper first: python main.py")
            return

    job = dict(job_row)
    print(f"\nJob: {job['title']} @ {job['company']}")
    print(f"Source: {job['source']}")
    print(f"Score: {job.get('score', 'N/A')}")
    print(f"URL: {job['url'][:80]}")

    if job["source"] not in ("greenhouse", "lever"):
        print(f"\n⚠ Source '{job['source']}' is not supported for auto-apply (only greenhouse/lever).")
        print("  Add --job <id> to test a specific greenhouse or lever job.")
        return

    if not dry_run:
        confirm = input("\n*** LIVE MODE — this will ACTUALLY SUBMIT an application. Continue? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return

    print(f"\nRunning {'dry run' if dry_run else 'live application'}...")
    print("-" * 50)

    # Mark as queued so run_application can update status
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO applications (user_id, job_id, status)
            VALUES ($1, $2, 'queued')
            ON CONFLICT (user_id, job_id) DO UPDATE SET status = 'queued'
        """, user_id, job["id"])

    await run_application(job, user_id=user_id, dry_run=dry_run)

    # Check final status
    async with pool.acquire() as conn:
        result = await conn.fetchrow(
            "SELECT status, notes FROM applications WHERE user_id=$1 AND job_id=$2",
            user_id, job["id"]
        )
    print("\n" + "=" * 50)
    if result:
        print(f"Final status : {result['status']}")
        print(f"Notes        : {result['notes']}")
    else:
        print("No application record found.")

    if dry_run:
        screenshots = [f for f in os.listdir("screenshots") if f.startswith("dry_run")]
        if screenshots:
            latest = sorted(screenshots)[-1]
            print(f"\nScreenshot   : screenshots/{latest}")
            print("  Open this file to verify the form was filled correctly.")


if __name__ == "__main__":
    asyncio.run(main())
