"""
One-time script to fetch descriptions for all jobs that have none.
Run once: python backfill_descriptions.py
"""
import asyncio
import httpx
import re
from db import get_pool


def clean_html(html: str) -> str:
    import html as html_module
    html = html_module.unescape(html)  # decode &lt;h2&gt; → <h2> (fixes double-encoding)
    html = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html, flags=re.DOTALL)
    return html[:5000]


async def fetch_greenhouse_desc(client, url: str) -> str:
    """Extract company slug + job ID from a Greenhouse URL and fetch description."""
    # URL patterns:
    # https://boards.greenhouse.io/company/jobs/12345
    # https://job-boards.greenhouse.io/company/jobs/12345
    m = re.search(r'greenhouse\.io/([^/]+)/jobs/(\d+)', url)
    if not m:
        return ""
    company, job_id = m.group(1), m.group(2)
    try:
        r = await client.get(
            f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{job_id}",
            timeout=10
        )
        if r.status_code == 200:
            return clean_html(r.json().get("content", ""))
    except Exception:
        pass
    return ""


async def fetch_lever_desc(client, url: str) -> str:
    """Fetch Lever job description via their API."""
    # URL: https://jobs.lever.co/company/uuid or https://lever.co/company/jobs/uuid
    m = re.search(r'lever\.co/([^/]+)/([a-f0-9-]{36})', url)
    if not m:
        return ""
    company, job_id = m.group(1), m.group(2)
    try:
        r = await client.get(
            f"https://api.lever.co/v0/postings/{company}/{job_id}",
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            return (data.get("description") or data.get("descriptionPlain", ""))[:5000]
    except Exception:
        pass
    return ""


async def main():
    pool = await get_pool()

    async with pool.acquire() as conn:
        jobs = await conn.fetch(
            "SELECT id, url, source FROM jobs WHERE description IS NULL OR description = '' ORDER BY id"
        )

    print(f"Found {len(jobs)} jobs with missing descriptions")
    if not jobs:
        print("Nothing to do!")
        return

    updated = 0
    failed = 0

    async with httpx.AsyncClient(timeout=15) as client:
        for i, job in enumerate(jobs):
            url = job["url"] or ""
            source = job["source"] or ""

            desc = ""
            if source == "greenhouse" or "greenhouse.io" in url:
                desc = await fetch_greenhouse_desc(client, url)
            elif source == "lever" or "lever.co" in url:
                desc = await fetch_lever_desc(client, url)

            if desc:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE jobs SET description = $1 WHERE id = $2",
                        desc, job["id"]
                    )
                updated += 1
                print(f"  [{i+1}/{len(jobs)}] ✓ job {job['id']}")
            else:
                failed += 1
                if (i + 1) % 20 == 0:
                    print(f"  [{i+1}/{len(jobs)}] ... ({failed} skipped so far)")

            # Small delay to avoid hammering APIs
            await asyncio.sleep(0.2)

    print(f"\nDone — updated: {updated}  |  no description found: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
