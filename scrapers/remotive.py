"""
Remotive — free public JSON API, no key needed.
Covers remote.co and remotive.com listings (remote-first companies globally).
"""
import httpx
import re
from db import insert_jobs_batch
from matcher import is_engineering_job


def _strip_html(html: str) -> str:
    return re.sub(r'<[^>]+>', ' ', html or '')[:5000]


async def scrape_remotive():
    all_jobs = []
    async with httpx.AsyncClient(timeout=20) as client:
        for category in ["software-dev", "devops-sysadmin", "data"]:
            try:
                r = await client.get(
                    "https://remotive.com/api/remote-jobs",
                    params={"category": category, "limit": 100},
                )
                if r.status_code != 200:
                    continue
                for job in r.json().get("jobs", []):
                    title = job.get("title", "")
                    if not is_engineering_job(title):
                        continue
                    all_jobs.append({
                        "title": title,
                        "company": job.get("company_name", ""),
                        "location": job.get("candidate_required_location") or "Remote",
                        "url": job.get("url", ""),
                        "source": "remotive",
                        "description": _strip_html(job.get("description", "")),
                    })
            except Exception as e:
                print(f"  Remotive [{category}] error: {e}")

    await insert_jobs_batch(all_jobs)
    print(f"  Remotive: {len(all_jobs)} jobs")
    return len(all_jobs)
