import asyncio
from config import DATABASE_URL, USER_EMAIL, USER_NAME
from db import get_pool, get_or_create_user


async def show_results():
    if not DATABASE_URL:
        print("Set DATABASE_URL in .env")
        return

    user_id = await get_or_create_user(USER_EMAIL, USER_NAME)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Summary stats (global jobs + this user's applications)
        total = (await conn.fetchrow("SELECT COUNT(*) AS total FROM jobs"))["total"]
        scored = (
            await conn.fetchrow(
                """
                SELECT COUNT(*) AS scored FROM applications
                WHERE user_id = $1 AND score IS NOT NULL
                """,
                user_id,
            )
        )["scored"]
        good = (
            await conn.fetchrow(
                """
                SELECT COUNT(*) AS good FROM applications
                WHERE user_id = $1 AND score >= 7
                """,
                user_id,
            )
        )["good"]

        print("\n" + "=" * 60)
        print("  JOB BOT — RESULTS DASHBOARD")
        print("=" * 60)
        print(f"  User               : {USER_NAME} <{USER_EMAIL}> (id={user_id})")
        print(f"  Total jobs in DB   : {total}")
        print(f"  Scored (you)       : {scored}")
        print(f"  Strong matches (7+): {good}")
        print("=" * 60)

        # Top jobs (this user's applications)
        print("\n  TOP MATCHES\n")
        jobs = await conn.fetch(
            """
            SELECT j.title, j.company, j.location, a.score, j.url
            FROM jobs j
            JOIN applications a ON a.job_id = j.id
            WHERE a.user_id = $1 AND a.score >= 6
            ORDER BY a.score DESC
            LIMIT 30
            """,
            user_id,
        )

        if not jobs:
            print("  No strong matches yet. Try lowering the score threshold.")
            return

        for i, job in enumerate(jobs, 1):
            bar = "█" * job["score"] + "░" * (10 - job["score"])
            print(f"  {i:>2}. [{bar}] {job['score']}/10")
            print(f"      {job['title']} @ {job['company']}")
            print(f"      {job['location'] or 'Location not listed'}")
            print(f"      {job['url']}")
            print()

        # By company breakdown
        print("=" * 60)
        print("  JOBS BY COMPANY (score 6+)\n")
        companies = await conn.fetch(
            """
            SELECT j.company, COUNT(*) AS count, ROUND(AVG(a.score)::numeric, 1) AS avg_score
            FROM jobs j
            JOIN applications a ON a.job_id = j.id
            WHERE a.user_id = $1 AND a.score >= 6
            GROUP BY j.company
            ORDER BY count DESC
            """,
            user_id,
        )

        for row in companies:
            print(
                f"  {row['company']:<20} {row['count']} jobs  (avg score: {row['avg_score']})"
            )

        print("\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(show_results())
